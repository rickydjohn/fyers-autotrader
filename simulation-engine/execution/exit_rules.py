"""
Exit rules — premium-first architecture.

For option trades, exit is driven entirely by option premium movement.
Index levels provide awareness context at profit milestones but never
trigger exits directly.

Rule priority:
  1. SESSION_CLOSE  — 15:00 IST force close (always applies)
  2. STOP_LOSS      — option LTP ≤ entry × 0.90  (−10% hard stop)
  3. PA_RESISTANCE  — CE: underlying within 0.20% of nearest resistance/day_high/PDH
     PA_SUPPORT     — PE: underlying within 0.20% of nearest support/day_low/PDL
                      Only fires when position is currently in profit (locks in gains)
  4. DELTA_ERODED   — |delta| < 0.20 (option far OTM, premium bleeding pointlessly)
  5. IV_CRUSH       — IV fell >20% from entry (vega working against us)
  6. TRAIL_FLOOR    — option LTP ≤ peak − (entry × 5%), active after trail engaged
  7. MILESTONE      — TRENDING day: at +15% and every +10% of entry thereafter:
                         indicators confirmed  → trail continues (no exit)
                         indicators not confirmed → lock in gains at milestone (exit)
                       RANGING day: at +10%, always exit immediately (no trail)

For non-option trades (underlying/equity directly):
  8. STOPPED        — underlying LTP crossed stop_loss
  9. CLOSED         — underlying LTP crossed target

Rationale for premium-first:
  Index levels are wrong for options because theta decay, IV collapse, and
  delta erosion can destroy option value while the index stays completely
  flat — well inside any index-level stop.  The option's own price is the
  only reliable signal.
"""

import logging
from datetime import datetime
from typing import Optional, Tuple

import pytz

from config import settings
from models.schemas import Position

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# ── Thresholds ────────────────────────────────────────────────────────────────
SESSION_CLOSE_HOUR: int    = settings.session_close_hour
SESSION_CLOSE_MINUTE: int  = settings.session_close_minute

PREMIUM_SL_PCT: float         = 0.10   # hard stop: exit if option loses ≥10% of entry premium
FIRST_MILESTONE_PCT: float    = 0.15   # TRENDING: trail activates at +15% gain from entry
RANGING_MILESTONE_PCT: float  = 0.10   # RANGING: exit immediately at +10% gain from entry
MILESTONE_STEP_PCT: float     = 0.10   # subsequent milestones every +10% of entry
TRAIL_OFFSET_PCT: float       = 0.05   # trail floor = peak_price × (1 − 5%)

DELTA_EROSION_MIN: float   = 0.20   # exit if |delta| drops below this
IV_CRUSH_THRESHOLD: float  = 0.80   # exit if iv < entry_iv × this

# Price action exit thresholds
PA_RESISTANCE_PROXIMITY: float = 0.0025  # exit CE if underlying within 0.25% of resistance
PA_SUPPORT_PROXIMITY: float    = 0.0025  # exit PE if underlying within 0.25% of support
# Minimum gross gain (option_ltp − entry) × qty before PA_RESISTANCE/PA_SUPPORT fires.
# Prevents commission-eating exits where the option barely moved. ₹60 covers round-trip
# commission (₹40) + exit slippage (≈₹13 BNF / ≈₹5 NIFTY) with a small safety margin.
PA_MIN_GROSS_PROFIT: float = 60.0


def _indicators_confirm(side: str, indicators: dict) -> bool:
    """
    2-of-3 indicator check used at each profit milestone to decide whether to
    continue trailing or lock in gains.

    CE / BUY  → needs bullish conditions: RSI > 50, price > VWAP, MACD hist > 0
    PE / SELL → needs bearish conditions: RSI < 50, price < VWAP, MACD hist < 0

    Missing indicators are treated as neutral (neither confirm nor deny).
    Returns True when at least 2 of the 3 conditions are met.
    """
    rsi  = float(indicators.get("rsi")         or 50)
    vwap = float(indicators.get("vwap")        or 0)
    ltp  = float(indicators.get("ltp")         or 0)
    macd = float(indicators.get("macd")        or 0)
    sig  = float(indicators.get("macd_signal") or 0)
    hist = macd - sig

    if side == "BUY":   # CE — bullish
        votes = [rsi > 50, (ltp > vwap if vwap else False), hist > 0]
    else:               # PE — bearish
        votes = [rsi < 50, (ltp < vwap if vwap else False), hist < 0]

    confirmed = sum(votes) >= 2
    logger.debug(
        f"Milestone indicator check ({side}): "
        f"RSI={rsi:.1f} {'✓' if votes[0] else '✗'}, "
        f"LTP vs VWAP={'above ✓' if votes[1] else 'below ✗' if vwap else 'N/A'}, "
        f"MACD hist={hist:.2f} {'✓' if votes[2] else '✗'} "
        f"→ {'CONFIRMED' if confirmed else 'NOT CONFIRMED'}"
    )
    return confirmed


def check_exit(
    pos: Position,
    underlying_ltp: float,
    option_ltp: Optional[float],
    greeks: Optional[dict],
    indicators: Optional[dict] = None,
    now: Optional[datetime] = None,
    market_context: Optional[dict] = None,
) -> Tuple[bool, str, float, int]:
    """
    Evaluate all exit conditions for an open position.

    Args:
        pos:            Open Position (entry_option_price, entry_iv, peak, milestone_count).
        underlying_ltp: Current index LTP — used as exit price for non-option trades
                        and as context for milestone indicator checks.
        option_ltp:     Current option LTP (None if not holding an option).
        greeks:         Greeks dict (delta, iv, etc.) — None if unavailable.
        indicators:     Index indicators for milestone confirmation
                        (keys: rsi, vwap, ltp, macd, macd_signal).
        now:            Current IST datetime (defaults to datetime.now(IST)).
        market_context: Current structural price levels from market snapshot
                        (day_high, day_low, prev_day_high, prev_day_low,
                         nearest_resistance, nearest_resistance_label,
                         nearest_support, nearest_support_label).

    Returns:
        (should_exit, exit_reason, exit_price, new_milestone_count)
        When should_exit is False and new_milestone_count > pos.milestone_count,
        the caller must write the updated count back to Redis.
    """
    if now is None:
        now = datetime.now(IST)
    if indicators is None:
        indicators = {}
    if market_context is None:
        market_context = {}

    is_option_position = bool(pos.option_symbol and pos.entry_option_price > 0)
    milestone          = pos.milestone_count

    # ── Guard: option position with stale/missing LTP ────────────────────────
    # If this is an option trade but option_ltp is unavailable (Redis key expired,
    # Fyers quote failed), do NOT fall through to using underlying_ltp as exit
    # price — that produces completely wrong PnL (e.g. BankNifty 54981 vs ₹1224).
    # Skip all rules this cycle, except SESSION_CLOSE which uses peak/entry fallback.
    if is_option_position and not option_ltp:
        if now.hour * 60 + now.minute >= SESSION_CLOSE_HOUR * 60 + SESSION_CLOSE_MINUTE:
            fallback = pos.peak_option_price if pos.peak_option_price > 0 else pos.entry_option_price
            logger.warning(
                f"[EXIT] SESSION_CLOSE — {pos.symbol}: option_ltp unavailable, "
                f"using last-known price ₹{fallback:.2f}"
            )
            return True, "SESSION_CLOSE", fallback, milestone
        logger.debug(
            f"[SKIP] {pos.symbol}: option_ltp unavailable this cycle — "
            f"skipping exit check (will retry next poll)"
        )
        return False, "", 0.0, milestone

    holding_option = is_option_position  # option_ltp is non-None here for option positions
    exit_price     = option_ltp if holding_option else underlying_ltp

    # ── Rule 1: Session close ─────────────────────────────────────────────────
    if now.hour * 60 + now.minute >= SESSION_CLOSE_HOUR * 60 + SESSION_CLOSE_MINUTE:
        logger.info(f"[EXIT] SESSION_CLOSE — {pos.symbol} at {now.strftime('%H:%M')}")
        return True, "SESSION_CLOSE", exit_price, milestone

    if holding_option:
        entry = pos.entry_option_price

        # ── Rule 2: Premium stop loss (−10%) ─────────────────────────────────
        # Exit at sl_floor, not option_ltp — price may gap through the floor
        # between polls; honouring the floor price caps max loss at −10%.
        sl_floor = entry * (1.0 - PREMIUM_SL_PCT)
        if option_ltp <= sl_floor:
            logger.info(
                f"[EXIT] STOP_LOSS — {pos.symbol}: "
                f"option ₹{option_ltp:.2f} ≤ floor ₹{sl_floor:.2f} "
                f"(entry ₹{entry:.2f}, −{PREMIUM_SL_PCT * 100:.0f}%)"
            )
            return True, "STOP_LOSS", sl_floor, milestone

        # ── Rule 3: Price action — resistance/support level reached ──────────
        if market_context and option_ltp and option_ltp > pos.entry_option_price:
            # Only fire when gross gain is large enough to survive exit costs.
            # Prevents commission-bleeding "profit lock" exits when the option barely
            # moved (e.g. ₹1.50 gross on a ₹40 round-trip commission = guaranteed loss).
            gross_gain = (option_ltp - pos.entry_option_price) * pos.quantity
            if gross_gain >= PA_MIN_GROSS_PROFIT:
                is_ce = pos.side == "BUY"
                # Levels to check: nearest S/R first, then day extremes, then PDH/PDL
                if is_ce:
                    resistance_levels = [
                        (market_context.get("nearest_resistance", 0),
                         market_context.get("nearest_resistance_label", "resistance")),
                        (market_context.get("day_high", 0),     "day_high"),
                        (market_context.get("prev_day_high", 0), "PDH"),
                    ]
                    for level, label in resistance_levels:
                        if level > 0 and underlying_ltp >= level * (1 - PA_RESISTANCE_PROXIMITY):
                            logger.info(
                                f"[EXIT] PA_RESISTANCE — {pos.symbol}: "
                                f"underlying ₹{underlying_ltp:.2f} at {label} ₹{level:.2f} "
                                f"(within {PA_RESISTANCE_PROXIMITY*100:.2f}%), "
                                f"option ₹{option_ltp:.2f} > entry ₹{pos.entry_option_price:.2f} "
                                f"gross=₹{gross_gain:.0f}, locking in profit"
                            )
                            return True, "PA_RESISTANCE", option_ltp, milestone
                else:
                    support_levels = [
                        (market_context.get("nearest_support", 0),
                         market_context.get("nearest_support_label", "support")),
                        (market_context.get("day_low", 0),      "day_low"),
                        (market_context.get("prev_day_low", 0), "PDL"),
                        (market_context.get("prev_day_high", 0), "PDH"),
                    ]
                    for level, label in support_levels:
                        if level > 0 and underlying_ltp <= level * (1 + PA_SUPPORT_PROXIMITY):
                            logger.info(
                                f"[EXIT] PA_SUPPORT — {pos.symbol}: "
                                f"underlying ₹{underlying_ltp:.2f} at {label} ₹{level:.2f} "
                                f"(within {PA_SUPPORT_PROXIMITY*100:.2f}%), "
                                f"option ₹{option_ltp:.2f} > entry ₹{pos.entry_option_price:.2f} "
                                f"gross=₹{gross_gain:.0f}, locking in profit"
                            )
                            return True, "PA_SUPPORT", option_ltp, milestone

        if greeks:
            # ── Rule 4: Delta erosion ─────────────────────────────────────────
            delta = abs(float(greeks.get("delta", 1.0) or 1.0))
            # delta == 1.0 default means "data missing — don't trigger"
            if 0 < delta < DELTA_EROSION_MIN:
                logger.info(
                    f"[EXIT] DELTA_ERODED — {pos.symbol}: "
                    f"|delta|={delta:.3f} < threshold {DELTA_EROSION_MIN}"
                )
                return True, "DELTA_ERODED", option_ltp, milestone

            # ── Rule 5: IV crush ──────────────────────────────────────────────
            iv = float(greeks.get("iv", 0) or 0)
            if iv > 0 and pos.entry_iv > 0 and iv < pos.entry_iv * IV_CRUSH_THRESHOLD:
                logger.info(
                    f"[EXIT] IV_CRUSH — {pos.symbol}: "
                    f"iv={iv:.1f}% vs entry_iv={pos.entry_iv:.1f}% "
                    f"(threshold {IV_CRUSH_THRESHOLD * 100:.0f}%)"
                )
                return True, "IV_CRUSH", option_ltp, milestone

        # ── Rule 5: Trail floor (active only once milestone_count > 0) ───────
        if milestone > 0 and pos.peak_option_price > 0:
            trail_floor = pos.peak_option_price * (1.0 - TRAIL_OFFSET_PCT)
            if option_ltp <= trail_floor:
                logger.info(
                    f"[EXIT] TRAIL_FLOOR — {pos.symbol}: "
                    f"option ₹{option_ltp:.2f} ≤ floor ₹{trail_floor:.2f} "
                    f"(peak ₹{pos.peak_option_price:.2f} × {(1 - TRAIL_OFFSET_PCT) * 100:.0f}%)"
                )
                return True, "TRAIL_STOP", option_ltp, milestone

        # ── Rule 6: Milestone check ───────────────────────────────────────────
        # Day type determines behaviour:
        #   RANGING  → first milestone at +10%, exit immediately (no indicator
        #               check, no trailing) — range-bound days are mean-reverting
        #   TRENDING → first milestone at +20%, indicators checked; trail if
        #               confirmed, lock in gains if not
        is_ranging = pos.day_type == "RANGING"
        first_milestone_pct = RANGING_MILESTONE_PCT if is_ranging else FIRST_MILESTONE_PCT
        next_target = entry * (1.0 + first_milestone_pct + milestone * MILESTONE_STEP_PCT)
        if option_ltp >= next_target:
            new_milestone = milestone + 1
            gain_pct = (option_ltp / entry - 1) * 100
            if is_ranging:
                logger.info(
                    f"[MILESTONE EXIT {new_milestone}] {pos.symbol}: "
                    f"option ₹{option_ltp:.2f} (+{gain_pct:.0f}%) — "
                    f"RANGING day, locking in gains immediately"
                )
                return True, "CLOSED", option_ltp, new_milestone
            else:
                confirmed = _indicators_confirm(pos.side, indicators)
                if confirmed:
                    logger.info(
                        f"[MILESTONE {new_milestone}] {pos.symbol}: "
                        f"option ₹{option_ltp:.2f} (+{gain_pct:.0f}%) — "
                        f"indicators confirmed, trail continues"
                    )
                    return False, "", 0.0, new_milestone
                else:
                    logger.info(
                        f"[MILESTONE EXIT {new_milestone}] {pos.symbol}: "
                        f"option ₹{option_ltp:.2f} (+{gain_pct:.0f}%) — "
                        f"indicators not confirmed, locking in gains"
                    )
                    return True, "CLOSED", option_ltp, new_milestone

        return False, "", 0.0, milestone

    else:
        # ── Rules 7 & 8: Non-option (equity / direct index) ──────────────────
        if pos.side == "BUY":
            if underlying_ltp <= pos.stop_loss:
                return True, "STOPPED", underlying_ltp, milestone
            if underlying_ltp >= pos.target:
                return True, "CLOSED", underlying_ltp, milestone
        else:
            if underlying_ltp >= pos.stop_loss:
                return True, "STOPPED", underlying_ltp, milestone
            if underlying_ltp <= pos.target:
                return True, "CLOSED", underlying_ltp, milestone

        return False, "", 0.0, milestone
