"""
Simulation Engine - subscribes to core-engine decisions and executes mock trades.
"""

import asyncio
import json
import logging
import sys
import time
from contextlib import asynccontextmanager

from datetime import datetime

import httpx
import pytz
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from analytics.pnl import compute_pnl_summary, get_all_trades, get_open_positions
from config import settings
from execution import mock_broker, live_broker
from execution.exit_rules import check_exit, PREMIUM_SL_PCT, FIRST_MILESTONE_PCT
from execution.invalidation_exit import check_invalidation_exit
from models.schemas import Position
from portfolio.budget import initialize_budget, load_budget, reconcile_invested
import data_client

IST = pytz.timezone("Asia/Kolkata")

logging.basicConfig(
    stream=sys.stdout,
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

redis_client: aioredis.Redis = None
_consumer_task: asyncio.Task = None
_last_fyers_reconcile: float = 0.0


class ManualCloseRequest(BaseModel):
    exit_reason: str = "MANUAL_UI_EXIT"


async def _reconcile_fyers_positions() -> None:
    """Detect positions closed externally on Fyers and record them with USER_EXIT_FYERS tag."""
    positions_raw = await redis_client.hgetall("positions:open")
    if not positions_raw:
        return

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{settings.core_engine_url}/fyers/positions")
            if r.status_code != 200:
                logger.warning(f"Fyers reconcile: core-engine returned {r.status_code}")
                return
            fyers_positions = r.json().get("positions", [])
    except Exception as e:
        logger.warning(f"Fyers reconcile request failed: {e}")
        return

    fyers_symbols = {p.get("symbol") for p in fyers_positions if p.get("symbol")}

    for symbol, pos_data in positions_raw.items():
        try:
            pos = Position(**json.loads(pos_data))

            # Skip positions opened less than 120 seconds ago.
            # Fyers positions API can lag a new order by several seconds; reconciling
            # too soon causes a false external-close (USER_EXIT_FYERS) race condition.
            pos_age_s = (datetime.now(IST) - pos.entry_time.astimezone(IST)).total_seconds()
            if pos_age_s < 120:
                logger.debug(f"Reconcile: skipping {symbol} (age {pos_age_s:.0f}s < 120s)")
                continue

            # For option positions, match against the option symbol on Fyers,
            # not the underlying symbol used as the Redis key.
            fyers_lookup = pos.option_symbol if pos.option_symbol else symbol
            if fyers_lookup not in fyers_symbols:
                # For option positions, use the option LTP not the underlying spot price
                opt_sym = pos.option_symbol if hasattr(pos, "option_symbol") and pos.option_symbol else None
                ltp_raw = None
                if opt_sym:
                    ltp_raw = (
                        await redis_client.get(f"ltp:{opt_sym}")
                        or await redis_client.get(f"market:{opt_sym}")
                    )
                if not ltp_raw:
                    ltp_raw = (
                        await redis_client.get(f"ltp:{symbol}")
                        or await redis_client.get(f"market:{symbol}")
                    )
                exit_price = float(json.loads(ltp_raw).get("ltp", 0)) if ltp_raw else 0.0
                await live_broker.record_external_close(
                    redis_client, symbol, exit_price, exit_reason="USER_EXIT_FYERS"
                )
                logger.info(f"Reconcile: external close detected for {symbol} @ ₹{exit_price:.2f}")
        except Exception as e:
            logger.exception(f"Reconcile error for {symbol}: {e}")


async def _startup_fyers_reconcile() -> None:
    """
    On startup, check if Fyers has any open positions that Redis doesn't know about.
    This only matters if Redis was wiped while a position was still open — a rare edge case,
    since Redis persists across normal container restarts.

    Flow:
      1. Skip if Redis already has tracked positions (nothing to recover).
      2. Fetch Fyers positions once (single API call).
      3. For each Fyers position, try to reconstruct the Position from data-service trade history.
      4. Write recovered positions back to Redis so dedup and exit-rules work correctly.
    """
    mode = await redis_client.get("trading:mode") or "simulation"
    if mode != "live":
        return

    if await redis_client.hlen("positions:open") > 0:
        logger.info("Startup reconcile: Redis has open positions, skipping Fyers sync")
        return

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{settings.core_engine_url}/fyers/positions")
            if r.status_code != 200:
                logger.warning(
                    f"Startup reconcile: core-engine returned {r.status_code} — "
                    f"will retry after authentication"
                )
                await redis_client.set("reconcile:pending", "1")
                return
            fyers_positions = r.json().get("positions", [])
    except Exception as e:
        logger.warning(
            f"Startup reconcile: Fyers fetch failed ({e}) — will retry after authentication"
        )
        await redis_client.set("reconcile:pending", "1")
        return

    # Successful fetch — clear any pending flag regardless of whether positions were found
    await redis_client.delete("reconcile:pending")

    if not fyers_positions:
        logger.info("Startup reconcile: no open positions on Fyers")
        return

    logger.warning(
        f"Startup reconcile: {len(fyers_positions)} Fyers position(s) found with empty Redis — "
        f"attempting reconstruction"
    )

    # Fetch recent open trades from data-service for accurate reconstruction
    open_trades_by_option: dict = {}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r2 = await client.get(f"{settings.data_service_url}/api/v1/trades")
            if r2.status_code == 200:
                for t in r2.json().get("data", {}).get("trades", []):
                    if t.get("status") == "OPEN" and t.get("option_symbol"):
                        open_trades_by_option[t["option_symbol"]] = t
    except Exception as e:
        logger.warning(f"Startup reconcile: data-service lookup failed: {e}")

    for fp in fyers_positions:
        option_sym = fp.get("symbol", "")
        if not option_sym:
            continue

        # Derive the underlying Redis key from the option symbol
        if "NIFTYBANK" in option_sym:
            underlying = "NSE:NIFTYBANK-INDEX"
        elif "NIFTY" in option_sym:
            underlying = "NSE:NIFTY50-INDEX"
        else:
            logger.warning(f"Startup reconcile: cannot derive underlying for {option_sym}, skipping")
            continue

        buy_avg = float(fp.get("buyAvg") or 0)
        net_qty = abs(int(fp.get("netQty") or 0))
        if buy_avg <= 0 or net_qty <= 0:
            logger.warning(f"Startup reconcile: invalid Fyers data for {option_sym}, skipping")
            continue

        trade = open_trades_by_option.get(option_sym)
        if trade:
            entry_price = float(trade.get("entry_price", buy_avg))
            entry_time = datetime.fromisoformat(trade["entry_time"])
            decision_id = trade.get("decision_id", "RECOVERED")
            option_strike = trade.get("option_strike")
            option_type = trade.get("option_type", "CE")
            option_expiry = trade.get("option_expiry")
            day_type = "TRENDING"
        else:
            # No data-service record — use Fyers data as best approximation
            entry_price = buy_avg
            entry_time = datetime.now(IST)
            decision_id = "RECOVERED"
            option_strike = None
            option_type = "CE"
            option_expiry = None
            day_type = "TRENDING"

        position = Position(
            symbol=underlying,
            side="BUY",
            quantity=net_qty,
            avg_price=entry_price,
            entry_time=entry_time,
            stop_loss=round(entry_price * (1.0 - PREMIUM_SL_PCT), 2),
            target=round(entry_price * (1.0 + FIRST_MILESTONE_PCT), 2),
            decision_id=decision_id,
            option_symbol=option_sym,
            option_strike=option_strike,
            option_type=option_type,
            option_expiry=option_expiry,
            entry_option_price=entry_price,
            day_type=day_type,
        )
        await redis_client.hset("positions:open", underlying, position.model_dump_json())
        logger.warning(
            f"Startup reconcile: Recovered {underlying} → {option_sym} "
            f"@ ₹{entry_price:.2f} × {net_qty} lots "
            f"({'from data-service' if trade else 'from Fyers data only'})"
        )


async def _consume_decisions() -> None:
    """
    Subscribe to Redis 'decisions' stream.
    Executes BUY/SELL trades and manages stop-loss / target monitoring.
    """
    last_id = "$"  # only new messages after startup
    logger.info("Decision consumer started, waiting for signals...")

    while True:
        try:
            messages = await redis_client.xread(
                {"decisions": last_id},
                count=10,
                block=5000,  # block 5s, then loop
            )
            if not messages:
                await _check_stop_targets()
                continue

            for stream, entries in messages:
                for entry_id, data in entries:
                    last_id = entry_id
                    await _handle_decision(data)

            await _check_stop_targets()

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception(f"Consumer error: {e}")
            await asyncio.sleep(2)


async def _resolve_close_price(pos: Position, underlying_ltp: float) -> float:
    """
    Return the correct exit price for a position being closed on a signal flip.

    For option positions the exit price must be the option's current premium,
    not the underlying index LTP.  Using the index level (e.g. BANKNIFTY 56138)
    as the exit price for an option premium (e.g. ₹835) produces completely
    wrong P&L and inflates the trade record by 60×.

    Priority:
      1. market:{option_symbol}  — written every 10s by fast_position_watcher
      2. Live Fyers fetch via _fetch_live_ltp (fallback when Redis key expired)
      3. entry_option_price      — last resort to avoid recording index price
    """
    if not pos.option_symbol:
        return underlying_ltp

    # Prefer WS-fed ltp:{option_symbol} (sub-second freshness); fall back to
    # the REST fast-watcher's market:{option_symbol}; then live Fyers fetch.
    opt_raw = (
        await redis_client.get(f"ltp:{pos.option_symbol}")
        or await redis_client.get(f"market:{pos.option_symbol}")
    )
    if opt_raw:
        ltp = json.loads(opt_raw).get("ltp")
        if ltp:
            return float(ltp)

    live = await _fetch_live_ltp(pos.option_symbol)
    if live:
        return live

    # Neither Redis nor Fyers could provide the option price.
    # Use the entry price so P&L shows 0 rather than a nonsensical index value.
    logger.warning(
        f"[FLIP CLOSE] Could not resolve option LTP for {pos.option_symbol} "
        f"— using entry price ₹{pos.entry_option_price:.2f} as fallback"
    )
    return pos.entry_option_price


async def _handle_decision(data: dict) -> None:
    symbol = data.get("symbol", "")
    decision = data.get("decision", "HOLD")
    decision_id = data.get("decision_id", "")
    reasoning = data.get("reasoning", "")
    confidence = float(data.get("confidence", 0.5))

    try:
        stop_loss = float(data.get("stop_loss", 0))
        target = float(data.get("target", 0))
    except (ValueError, TypeError):
        stop_loss = target = 0.0

    option_symbol = data.get("option_symbol") or None
    option_type = data.get("option_type") or None
    option_expiry = data.get("option_expiry") or None
    try:
        option_strike = int(float(data.get("option_strike", 0) or 0)) or None
        option_price = float(data.get("option_price", 0) or 0) or None
        option_lot_size = int(float(data.get("option_lot_size", 0) or 0)) or None
    except (ValueError, TypeError):
        option_strike = option_price = option_lot_size = None

    # Extract DTE from stream payload; fall back to computing from option_expiry for
    # backward compatibility with decisions published before this field was added.
    try:
        dte = int(float(data.get("dte", -1) or -1))
        if dte < 0:
            if option_expiry:
                from datetime import date as _date
                dte = max(0, (_date.fromisoformat(option_expiry) - _date.today()).days)
            else:
                dte = 99
    except Exception:
        dte = 99

    # Determine day type from the ATR-normalized day_type field in indicators snapshot.
    # NARROW → TRENDING (milestone trail at +20%); MODERATE/WIDE → RANGING (lock in at +10%).
    # Falls back to legacy cpr_width_pct threshold for decisions missing the day_type field.
    day_type: str = "TRENDING"
    try:
        ind_raw = data.get("indicators") or "{}"
        ind_dict = json.loads(ind_raw) if isinstance(ind_raw, str) else ind_raw
        dt = (ind_dict.get("day_type") or "").upper()
        if dt == "NARROW":
            day_type = "TRENDING"
        elif dt in ("MODERATE", "WIDE"):
            day_type = "RANGING"
        else:
            # Legacy fallback for older decisions that don't carry day_type
            cpr_width_pct = float(ind_dict.get("cpr_width_pct") or 0)
            if cpr_width_pct >= 0.25:
                day_type = "RANGING"
    except Exception:
        pass

    # Get current price from market snapshot
    market_raw = await redis_client.get(f"market:{symbol}")
    if not market_raw:
        logger.debug(f"No market data for {symbol}, skipping decision")
        return

    market = json.loads(market_raw)
    current_price = market.get("ltp", 0)
    if not current_price:
        return

    mode = await redis_client.get("trading:mode") or "simulation"
    broker = live_broker if mode == "live" else mock_broker

    logger.info(f"[{mode.upper()}] {decision} {symbol} @ ₹{current_price:.2f} (conf={confidence:.2f})")

    # 70% confidence floor: treat any BUY/SELL below this threshold as HOLD.
    # Eliminates low-conviction churn that loses to commission even when the
    # direction is right, without touching the stop-loss / exit-rules path.
    CONFIDENCE_FLOOR = 0.70
    if decision in ("BUY", "SELL") and confidence < CONFIDENCE_FLOOR:
        logger.info(
            f"[CONF FLOOR] {decision} {symbol} conf={confidence:.2f} < {CONFIDENCE_FLOOR} — skipped"
        )
        return

    # Drift veto + fresh-price refresh — runs BEFORE the other gates.
    # The market:{symbol} snapshot price reflects scan-time and may be tens of
    # seconds (or minutes on Ollama gpt-oss:120b) stale by the time we get here.
    # 1. If price drifted >drift_veto_pct AGAINST the signal between snapshot and now,
    #    skip the trade — the LLM's view is no longer the market's view.
    # 2. Otherwise, replace current_price with the live LTP so the downstream
    #    gates (ORB / CPR / consolidation / entry proximity) evaluate the
    #    *actual* market state at order time, not the stale scan-time view.
    #    A 2026-05-14 BANKNIFTY trade demonstrated the bug: CPR-TC threshold was
    #    53,413 but the scan-time price was 53,341 (gate passed); the real price
    #    at order fire was ~53,415 (gate would have blocked).
    # Fail-open on quote failure so a flaky Fyers call doesn't introduce a new outage mode.
    if decision in ("BUY", "SELL"):
        snapshot_price = float(ind_dict.get("price") or 0)
        if snapshot_price > 0:
            live_ltp = await _fetch_live_ltp(symbol)
            if live_ltp:
                drift = (live_ltp - snapshot_price) / snapshot_price
                threshold = settings.drift_veto_pct
                adverse = (
                    (decision == "BUY"  and drift < -threshold) or
                    (decision == "SELL" and drift >  threshold)
                )
                if adverse:
                    logger.info(
                        f"[DRIFT VETO] {decision} {symbol}: snapshot ₹{snapshot_price:.2f} "
                        f"→ live ₹{live_ltp:.2f} ({drift*100:+.3f}%) — adverse drift exceeds "
                        f"{threshold*100:.2f}%, skipped"
                    )
                    return
                if abs(live_ltp - current_price) > 0.01:
                    logger.info(
                        f"[FRESH PRICE] {decision} {symbol}: snapshot ₹{current_price:.2f} → "
                        f"live ₹{live_ltp:.2f} (Δ {(live_ltp-current_price)/current_price*100:+.3f}%); "
                        f"gates will use the live price"
                    )
                current_price = live_ltp

    # ORB gate — no trades before 09:30 IST.
    # The opening 15 minutes (09:15–09:30) are used to form the session's range.
    # The market may trend, reverse, or break out after that — we don't impose
    # a directional requirement. We just refuse to trade while range is forming.
    if decision in ("BUY", "SELL"):
        now_ist = datetime.now(IST)
        orb_close_time = now_ist.replace(hour=9, minute=30, second=0, microsecond=0)
        if now_ist < orb_close_time:
            logger.info(
                f"[ORB GATE] {decision} {symbol}: before 09:30 IST — "
                f"opening range not yet established, skipped"
            )
            return

    # ORB breakout gate — block entries when price is still inside the opening range.
    # The 09:15–09:30 high/low define the session's opening range.  A trade in the direction
    # of the signal is only valid once price has cleared that range by 0.20% (data-derived
    # buffer from 141 days of NIFTY/BANKNIFTY history — inflection point for ~80% continuation).
    # BUY requires price > orb_high × 1.002 (bullish breakout above range)
    # SELL requires price < orb_low  × 0.998 (bearish breakdown below range)
    if decision in ("BUY", "SELL"):
        orb_high = float(ind_dict.get("orb_high") or 0)
        orb_low  = float(ind_dict.get("orb_low")  or 0)
        ORB_BUFFER = 0.002
        if orb_high > 0 and orb_low > 0:
            if decision == "BUY" and current_price <= orb_high * (1 + ORB_BUFFER):
                logger.info(
                    f"[ORB GATE] BUY {symbol}: price ₹{current_price:.2f} not above "
                    f"ORB high ₹{orb_high:.2f} +{ORB_BUFFER*100:.2f}% — no breakout confirmed, skipped"
                )
                return
            elif decision == "SELL" and current_price >= orb_low * (1 - ORB_BUFFER):
                logger.info(
                    f"[ORB GATE] SELL {symbol}: price ₹{current_price:.2f} not below "
                    f"ORB low ₹{orb_low:.2f} -{ORB_BUFFER*100:.2f}% — no breakdown confirmed, skipped"
                )
                return

    # CPR gate — price must have cleared the CPR band by 0.20% before entry.
    # Uses max(TC, BC) as the upper boundary and min(TC, BC) as the lower boundary so
    # the gate works correctly for both normal CPR (TC > BC) and inverted CPR (BC > TC).
    # BUY requires price > upper × 1.002 — confirmed breakout above the band.
    # SELL requires price < lower × 0.998 — confirmed breakdown below the band.
    # Gate is skipped when either value is zero (CPR not yet computed).
    if decision in ("BUY", "SELL"):
        cpr_tc = float(ind_dict.get("cpr_tc") or 0)
        cpr_bc = float(ind_dict.get("cpr_bc") or 0)
        CPR_BUFFER = 0.002
        if cpr_tc > 0 and cpr_bc > 0:
            cpr_upper = max(cpr_tc, cpr_bc)
            cpr_lower = min(cpr_tc, cpr_bc)
            if decision == "BUY" and current_price <= cpr_upper * (1 + CPR_BUFFER):
                logger.info(
                    f"[CPR GATE] BUY {symbol}: price ₹{current_price:.2f} not above "
                    f"CPR upper ₹{cpr_upper:.2f} +{CPR_BUFFER*100:.2f}% — no confirmed breakout, skipped"
                )
                return
            elif decision == "SELL" and current_price >= cpr_lower * (1 - CPR_BUFFER):
                logger.info(
                    f"[CPR GATE] SELL {symbol}: price ₹{current_price:.2f} not below "
                    f"CPR lower ₹{cpr_lower:.2f} -{CPR_BUFFER*100:.2f}% — no confirmed breakdown, skipped"
                )
                return

    # Consolidation gate — block all entries while price is inside the consolidation range.
    # range_breakout is BREAKOUT_HIGH or BREAKOUT_LOW when price clears the band with buffer;
    # NONE means either not consolidating or still inside the range. Only the latter case
    # (is_consolidating=True AND NONE) should block — wide non-consolidating markets are fine.
    if decision in ("BUY", "SELL"):
        range_breakout = ind_dict.get("range_breakout", "")
        consolidation_pct = float(ind_dict.get("consolidation_pct") or 1.0)
        is_consolidating = consolidation_pct < 0.40
        if is_consolidating and range_breakout == "NONE":
            logger.info(
                f"[CONSOLIDATION GATE] {decision} {symbol}: price inside consolidation range "
                f"({consolidation_pct:.1%} of ATR) — no breakout confirmed, skipped"
            )
            return

    # Entry proximity gate — block when the next level in the trade direction is too close.
    # For CE (BUY): if the nearest level above is within 0.25%, there is no room to run.
    # For PE (SELL): if the nearest level below is within 0.25%, there is no room to fall.
    # Levels are direction-agnostic (PDH, PDL, CPR, R1-R3, S1-S3, Pivot) — whatever the
    # pivot calc assigned as nearest_resistance / nearest_support.
    # Falls back to decision-time indicators when the live market snapshot is missing the
    # field, so the gate is never silently skipped due to a stale Redis key.
    PA_PROXIMITY = 0.0025
    mkt_ind = market.get("indicators", {})
    if decision == "BUY":
        nr = float(mkt_ind.get("nearest_resistance") or ind_dict.get("nearest_resistance") or 0)
        nr_label = mkt_ind.get("nearest_resistance_label") or ind_dict.get("nearest_resistance_label") or "level"
        # DayHigh is a running intraday extreme — on a trending bull day it equals current
        # price and would block every entry. Only static levels (PDH, CPR, pivots) are valid
        # entry gates. Same reasoning as exit_rules.py which also excludes DayHigh/DayLow.
        if nr > 0 and nr_label != "DayHigh" and nr * (1 - PA_PROXIMITY) <= current_price <= nr:
            logger.info(
                f"[ENTRY BLOCK] BUY {symbol}: underlying ₹{current_price:.2f} within "
                f"{PA_PROXIMITY*100:.2f}% of {nr_label} ₹{nr:.2f} — no room to run, skipped"
            )
            return

    elif decision == "SELL":
        ns = float(mkt_ind.get("nearest_support") or ind_dict.get("nearest_support") or 0)
        ns_label = mkt_ind.get("nearest_support_label") or ind_dict.get("nearest_support_label") or "level"
        # DayLow is a running intraday extreme — on a trending bear day it equals current
        # price and would block every SELL entry. Exclude it; PDL and pivot levels are enough.
        if ns > 0 and ns_label != "DayLow" and ns <= current_price <= ns * (1 + PA_PROXIMITY):
            logger.info(
                f"[ENTRY BLOCK] SELL {symbol}: underlying ₹{current_price:.2f} within "
                f"{PA_PROXIMITY*100:.2f}% of {ns_label} ₹{ns:.2f} — no room to fall, skipped"
            )
            return

    # Snapshot the index levels the LLM's thesis was built on. These get
    # frozen onto the position so the tick-driven invalidation-exit watcher
    # can detect when price has crossed back through them and the thesis is
    # broken. Coerced to float / None to keep the Pydantic dict[str,float]
    # contract clean.
    def _f(key: str) -> float | None:
        v = ind_dict.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None
    invalidation_levels = {
        k: v for k, v in {
            "vwap":   _f("vwap"),
            "ema_21": _f("ema_21"),
            "cpr_tc": _f("cpr_tc"),
            "cpr_bc": _f("cpr_bc"),
        }.items() if v is not None
    } or None

    if decision == "BUY":
        existing = await redis_client.hget("positions:open", symbol)
        if existing:
            pos = Position(**json.loads(existing))
            if pos.side == "SELL":
                # Signal flip: close bearish position, open bullish
                close_price = await _resolve_close_price(pos, current_price)
                await broker.close_position(redis_client, symbol, close_price)
            else:
                # Already long — don't add to or replace an open BUY position
                logger.debug(f"[SKIP] BUY {symbol}: BUY position already open")
                return

        await broker.open_position(
            redis_client, symbol, "BUY", current_price,
            stop_loss, target, decision_id, reasoning,
            option_symbol=option_symbol, option_strike=option_strike,
            option_type=option_type, option_expiry=option_expiry,
            option_price=option_price, option_lot_size=option_lot_size,
            day_type=day_type, dte=dte,
            invalidation_levels=invalidation_levels,
        )

    elif decision == "SELL":
        existing = await redis_client.hget("positions:open", symbol)
        if existing:
            pos = Position(**json.loads(existing))
            if pos.side == "BUY":
                # Signal flip: close bullish position, open bearish
                close_price = await _resolve_close_price(pos, current_price)
                await broker.close_position(redis_client, symbol, close_price)
            else:
                # Already short — don't add to or replace an open SELL position
                logger.debug(f"[SKIP] SELL {symbol}: SELL position already open")
                return

        await broker.open_position(
            redis_client, symbol, "SELL", current_price,
            stop_loss, target, decision_id, reasoning,
            option_symbol=option_symbol, option_strike=option_strike,
            option_type=option_type, option_expiry=option_expiry,
            option_price=option_price, option_lot_size=option_lot_size,
            day_type=day_type, dte=dte,
            invalidation_levels=invalidation_levels,
        )

    elif decision == "HOLD":
        pass


async def _fetch_live_ltp(symbol: str) -> float | None:
    """
    Return a real-time LTP, preferring the WebSocket-populated Redis cache.

    Path 1 — WS cache (ltp:{symbol}): if core-engine's FyersTickFeed has written
      a recent tick (≤5s old per the stored `ts`), use it. Sub-millisecond
      Redis read, no Fyers round-trip.
    Path 2 — REST fallback via core-engine /fyers/quote: used when the WS
      cache is missing or stale (WS down, market closed, or symbol not
      subscribed). Writes the result back to ltp:{symbol} for next time.
    """
    try:
        cached = await redis_client.get(f"ltp:{symbol}")
        if cached:
            data = json.loads(cached)
            ts_ms = int(data.get("ts") or 0)
            age_ms = int(time.time() * 1000) - ts_ms
            ltp = data.get("ltp")
            if ts_ms and age_ms <= 5_000 and ltp:
                return float(ltp)
    except Exception as e:
        logger.debug(f"WS cache read failed for {symbol}: {e}")

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{settings.core_engine_url}/fyers/quote/{symbol}")
            if r.status_code != 200:
                logger.warning(f"Live LTP fetch failed for {symbol}: HTTP {r.status_code}")
                return None
            ltp = r.json().get("ltp")
            if ltp:
                await redis_client.setex(
                    f"ltp:{symbol}", 30,
                    json.dumps({"ltp": ltp, "ts": int(time.time() * 1000)}),
                )
            return float(ltp) if ltp else None
    except Exception as e:
        logger.warning(f"Live LTP fetch error for {symbol}: {e}")
        return None


async def _check_stop_targets() -> None:
    """
    Evaluate exit conditions for all open positions.
    Runs on every consumer loop tick (~5s). Underlying + option prices are
    kept fresh by FyersTickFeed in core-engine (~200ms WS cadence); Greeks
    are refreshed by _fast_position_watcher (every GREEKS_POLL_INTERVAL_SECONDS).
    """
    positions_raw = await redis_client.hgetall("positions:open")
    if not positions_raw:
        return

    now = datetime.now(IST)
    mode = await redis_client.get("trading:mode") or "simulation"
    broker = live_broker if mode == "live" else mock_broker

    for symbol, pos_data in positions_raw.items():
        try:
            pos = Position(**json.loads(pos_data))

            # Underlying LTP — prefer ltp:{symbol} (WS-fed, ~200ms cadence,
            # 30s TTL); fall back to the full market: snapshot the LLM scan
            # writes every LLM_DECISION_INTERVAL_SECONDS.
            ltp_raw = await redis_client.get(f"ltp:{symbol}") or await redis_client.get(f"market:{symbol}")
            if ltp_raw:
                underlying_ltp = json.loads(ltp_raw).get("ltp", 0)
            else:
                underlying_ltp = 0

            if not underlying_ltp:
                # Redis keys expired while position is still open — this is unexpected.
                # Fetch live from Fyers rather than skipping exit evaluation.
                logger.warning(f"[SL-CHECK] No cached LTP for {symbol} with open position — fetching live from Fyers")
                underlying_ltp = await _fetch_live_ltp(symbol)
                if not underlying_ltp:
                    logger.error(f"[SL-CHECK] Could not obtain LTP for {symbol} — skipping tick")
                    continue

            # Option LTP — prefer the WS-fed ltp:{option_symbol} (sub-second),
            # fall back to the REST fast-watcher's market:{option_symbol} (5s),
            # and as a last resort hit Fyers directly via _fetch_live_ltp.
            # Greeks remain REST-only (Fyers WS doesn't push them).
            option_ltp: float | None = None
            greeks: dict | None = None
            if pos.option_symbol:
                opt_raw = await redis_client.get(f"ltp:{pos.option_symbol}")
                if not opt_raw:
                    opt_raw = await redis_client.get(f"market:{pos.option_symbol}")
                if opt_raw:
                    option_ltp = json.loads(opt_raw).get("ltp")
                greeks_raw = await redis_client.get(f"greeks:{pos.option_symbol}")
                if greeks_raw:
                    greeks = json.loads(greeks_raw)
                if not option_ltp:
                    logger.warning(f"[SL-CHECK] No cached LTP for option {pos.option_symbol} — fetching live from Fyers")
                    option_ltp = await _fetch_live_ltp(pos.option_symbol)

            # Index indicators for milestone confirmation (from full market snapshot)
            indicators: dict = {}
            market_context: dict = {}
            market_full_raw = await redis_client.get(f"market:{symbol}")
            if market_full_raw:
                mfull = json.loads(market_full_raw)
                ind = mfull.get("indicators", {})
                indicators = {
                    "rsi":         ind.get("rsi"),
                    "vwap":        ind.get("vwap"),
                    "ltp":         mfull.get("ltp"),
                    "macd":        ind.get("macd"),
                    "macd_signal": ind.get("macd_signal"),
                }
                market_context = {
                    "day_high":                  ind.get("day_high", 0),
                    "day_low":                   ind.get("day_low", 0),
                    "prev_day_high":             ind.get("prev_day_high", 0),
                    "prev_day_low":              ind.get("prev_day_low", 0),
                    "nearest_resistance":        ind.get("nearest_resistance", 0),
                    "nearest_resistance_label":  ind.get("nearest_resistance_label", ""),
                    "nearest_support":           ind.get("nearest_support", 0),
                    "nearest_support_label":     ind.get("nearest_support_label", ""),
                }

            # Track peak option price (update pos in memory; write-back deferred below)
            peak_updated = False
            if option_ltp and option_ltp > pos.peak_option_price:
                pos.peak_option_price = option_ltp
                peak_updated = True

            # ── Tick-driven invalidation exit ────────────────────────────────
            # Check this BEFORE the cost-based stops in check_exit. The trade's
            # thesis was built on price being on one side of VWAP/EMA21/CPR;
            # once price crosses back through, the thesis is broken and the
            # right action is to flatten regardless of where the option
            # premium is. Catches the 2026-05-14 BANKNIFTY53300PE pattern
            # where EMA21 was crossed 4 minutes before the option-SL hit.
            inv_reason = check_invalidation_exit(pos, underlying_ltp)
            if inv_reason:
                # Use option_ltp as exit price when holding an option, else
                # the underlying. Matches check_exit's exit_price logic.
                exit_px = option_ltp if (pos.option_symbol and option_ltp) else underlying_ltp
                logger.info(
                    f"[EXIT] {inv_reason} — {pos.symbol}: underlying ₹{underlying_ltp:.2f} "
                    f"crossed back through {inv_reason.split('_', 1)[1].lower()}"
                )
                await broker.close_position(
                    redis_client, symbol, exit_px,
                    status="STOPPED", exit_reason=inv_reason,
                )
                continue

            should_exit, reason, exit_price, new_milestone = check_exit(
                pos, underlying_ltp, option_ltp, greeks, indicators, now, market_context
            )

            if should_exit:
                # Map detailed reason to the CLOSED/STOPPED DB status enum
                db_status = "CLOSED" if reason == "CLOSED" else "STOPPED"
                await broker.close_position(
                    redis_client, symbol, exit_price,
                    status=db_status, exit_reason=reason,
                )
                # Block re-entry after a stop or trail exit to prevent overtrading.
                # STOP_LOSS: position went against us — cooldown prevents revenge trading.
                # TRAIL_STOP: trend reversed after profit peak — same cooldown prevents
                # immediately re-entering the same option on the next scan tick.
                if reason in ("STOP_LOSS", "TRAIL_STOP"):
                    await redis_client.setex(
                        f"sl:cooldown:{symbol}",
                        settings.sl_cooldown_minutes * 60,
                        "1",
                    )
            elif peak_updated or new_milestone != pos.milestone_count:
                # Write back peak and/or milestone advance in a single Redis call
                pos.milestone_count = new_milestone
                await redis_client.hset("positions:open", symbol, pos.model_dump_json())

        except Exception as e:
            logger.exception(f"Error checking stop/target for {symbol}: {e}")

    # Detect externally-closed Fyers positions (live mode only, every 30s).
    # Also retries startup reconciliation if the initial attempt failed (e.g. expired token).
    global _last_fyers_reconcile
    if mode == "live":
        now_ts = now.timestamp()
        if now_ts - _last_fyers_reconcile >= 30:
            _last_fyers_reconcile = now_ts
            try:
                await _reconcile_fyers_positions()
            except Exception as e:
                logger.warning(f"Fyers reconcile failed: {e}")

            # Retry startup reconcile if it previously failed (token was expired at boot)
            if await redis_client.exists("reconcile:pending"):
                logger.info("Startup reconcile: retrying after previous failure...")
                try:
                    await _startup_fyers_reconcile()
                except Exception as e:
                    logger.warning(f"Startup reconcile retry failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client, _consumer_task
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    await initialize_budget(redis_client)
    await reconcile_invested(redis_client)
    await _startup_fyers_reconcile()
    _consumer_task = asyncio.create_task(_consume_decisions())
    logger.info("Simulation engine started")
    yield
    _consumer_task.cancel()
    await data_client.close_client()
    await redis_client.aclose()
    logger.info("Simulation engine shutdown")


app = FastAPI(title="Simulation Engine", version="1.0.0", lifespan=lifespan)


@app.get("/healthz")
async def health_check():
    try:
        await redis_client.ping()
        return {"status": "ok", "checks": {"redis": "ok"}}
    except Exception:
        return {"status": "error", "checks": {"redis": "error"}}


@app.get("/positions")
async def get_positions():
    positions = await get_open_positions(redis_client)
    return {"positions": [p.model_dump() for p in positions]}


@app.get("/trades")
async def get_trades():
    trades = await get_all_trades(redis_client)
    return {"trades": [t.model_dump() for t in trades[:100]]}


@app.get("/pnl")
async def get_pnl():
    summary = await compute_pnl_summary(redis_client, current_prices={})
    return summary


@app.get("/budget")
async def get_budget():
    state = await load_budget(redis_client)
    return state.model_dump()


@app.post("/positions/{symbol}/close")
async def manual_close_position(symbol: str, body: ManualCloseRequest = ManualCloseRequest()):
    """Close an open position manually; records P&L with MANUAL_UI_EXIT tag."""
    existing = await redis_client.hget("positions:open", symbol)
    if not existing:
        raise HTTPException(status_code=404, detail=f"No open position for {symbol}")

    pos = Position(**json.loads(existing))

    # For option positions, look up the option's own LTP — not the underlying index.
    # Using the underlying LTP (e.g. NIFTY ~23800) as the exit price for an option
    # premium (~₹200) produces completely wrong P&L.
    if pos.option_symbol:
        opt_raw = await redis_client.get(f"market:{pos.option_symbol}")
        if not opt_raw:
            raise HTTPException(status_code=503, detail=f"No option market price available for {pos.option_symbol}")
        current_price = json.loads(opt_raw).get("ltp", 0.0)
        if not current_price:
            raise HTTPException(status_code=503, detail=f"Option market price is zero for {pos.option_symbol}")
    else:
        ltp_raw = (
            await redis_client.get(f"ltp:{symbol}")
            or await redis_client.get(f"market:{symbol}")
        )
        if not ltp_raw:
            raise HTTPException(status_code=503, detail=f"No market price available for {symbol}")
        current_price = json.loads(ltp_raw).get("ltp", 0.0)
        if not current_price:
            raise HTTPException(status_code=503, detail=f"Market price is zero for {symbol}")

    mode = await redis_client.get("trading:mode") or "simulation"
    broker = live_broker if mode == "live" else mock_broker

    trade = await broker.close_position(
        redis_client, symbol, current_price,
        status="STOPPED", exit_reason=body.exit_reason,
    )
    if trade is None:
        raise HTTPException(status_code=500, detail="Failed to close position")

    return {"status": "ok", "trade_id": trade.trade_id, "exit_price": current_price, "pnl": trade.pnl}
