"""
Exit rules — premium-first architecture.

For option trades, exit is driven entirely by option premium movement.
Index levels provide awareness context at profit milestones but never
trigger exits directly.

Rule priority:
  1. SESSION_CLOSE  — 15:00 IST force close (always applies)
  2. STOP_LOSS      — option LTP ≤ entry × 0.90  (−10% hard stop)
  3. DELTA_ERODED   — |delta| < 0.20 (option far OTM, premium bleeding pointlessly)
  4. IV_CRUSH       — IV fell >20% from entry (vega working against us)
  5. TRAIL_FLOOR    — option LTP ≤ peak − (entry × 5%), active after trail engaged
  6. MILESTONE      — at +20% and every +10% of entry thereafter:
                       indicators confirmed  → trail continues (no exit)
                       indicators not confirmed → lock in gains at milestone (exit)

For non-option trades (underlying/equity directly):
  7. STOPPED        — underlying LTP crossed stop_loss
  8. CLOSED         — underlying LTP crossed target

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

PREMIUM_SL_PCT: float      = 0.10   # hard stop: exit if option loses ≥10% of entry premium
FIRST_MILESTONE_PCT: float = 0.20   # trail activates at +20% gain from entry
MILESTONE_STEP_PCT: float  = 0.10   # subsequent milestones every +10% of entry
TRAIL_OFFSET_PCT: float    = 0.05   # trail floor = peak_price × (1 − 5%)

DELTA_EROSION_MIN: float   = 0.20   # exit if |delta| drops below this
IV_CRUSH_THRESHOLD: float  = 0.80   # exit if iv < entry_iv × this


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

    Returns:
        (should_exit, exit_reason, exit_price, new_milestone_count)
        When should_exit is False and new_milestone_count > pos.milestone_count,
        the caller must write the updated count back to Redis.
    """
    if now is None:
        now = datetime.now(IST)
    if indicators is None:
        indicators = {}

    holding_option = bool(pos.option_symbol and option_ltp and pos.entry_option_price > 0)
    exit_price     = option_ltp if holding_option else underlying_ltp
    milestone      = pos.milestone_count

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

        if greeks:
            # ── Rule 3: Delta erosion ─────────────────────────────────────────
            delta = abs(float(greeks.get("delta", 1.0) or 1.0))
            # delta == 1.0 default means "data missing — don't trigger"
            if 0 < delta < DELTA_EROSION_MIN:
                logger.info(
                    f"[EXIT] DELTA_ERODED — {pos.symbol}: "
                    f"|delta|={delta:.3f} < threshold {DELTA_EROSION_MIN}"
                )
                return True, "DELTA_ERODED", option_ltp, milestone

            # ── Rule 4: IV crush ──────────────────────────────────────────────
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
        # milestone 0 → first target at entry + 20%
        # milestone N → next target at entry + 20% + N × 10%
        next_target = entry * (1.0 + FIRST_MILESTONE_PCT + milestone * MILESTONE_STEP_PCT)
        if option_ltp >= next_target:
            new_milestone = milestone + 1
            confirmed = _indicators_confirm(pos.side, indicators)
            gain_pct = (option_ltp / entry - 1) * 100
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
