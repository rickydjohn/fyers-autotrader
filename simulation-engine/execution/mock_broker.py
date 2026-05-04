"""
Mock broker: simulates trade execution with realistic slippage and commission.
v2: Persists all trades to data-service (TimescaleDB) for durable storage.

Slippage model: 0.05% of entry price (configurable).
Commission model: max(flat_fee, pct_of_trade_value) — mirrors NSE discount brokers.
"""

import json
import logging
import uuid
from datetime import datetime
from typing import Optional

import pytz
import redis.asyncio as aioredis

from config import settings
from models.schemas import Position, Trade
from portfolio.budget import allocate, get_max_position_value, release
from notifications.slack import notify_trade_opened, notify_trade_closed
import data_client
from execution.exit_rules import PREMIUM_SL_PCT, FIRST_MILESTONE_PCT, RANGING_MILESTONE_PCT

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")


def _apply_slippage(price: float, side: str) -> float:
    slip = price * (settings.slippage_pct / 100)
    return price + slip if side == "BUY" else price - slip


def _calculate_commission(trade_value: float) -> float:
    pct_comm = trade_value * (settings.commission_pct / 100)
    return max(settings.commission_flat, pct_comm)


def _calculate_quantity(price: float, max_value: float) -> int:
    if price <= 0:
        return 0
    qty = int(max_value / price)
    return max(qty, 1)


async def open_position(
    redis_client: aioredis.Redis,
    symbol: str,
    side: str,
    price: float,
    stop_loss: float,
    target: float,
    decision_id: str,
    reasoning: str,
    option_symbol: Optional[str] = None,
    option_strike: Optional[int] = None,
    option_type: Optional[str] = None,
    option_expiry: Optional[str] = None,
    option_price: Optional[float] = None,
    option_lot_size: Optional[int] = None,
    day_type: Optional[str] = None,
) -> Optional[Trade]:
    """Open a new simulated position. Trades the option if one is provided."""
    # ── Gate 1: No new positions at or after session close ────────────────────
    _now = datetime.now(IST)
    if _now.hour * 60 + _now.minute >= settings.session_close_hour * 60 + settings.session_close_minute:
        logger.info(f"[GATE] Session closed ({_now.strftime('%H:%M')}) — skipping {symbol}")
        return None

    # ── Gate 2: Minimum option premium — SL is inside bid-ask below this ─────
    if option_symbol and option_price is not None and option_price < settings.min_option_premium:
        logger.info(
            f"[GATE] Premium ₹{option_price:.2f} < min ₹{settings.min_option_premium:.0f} "
            f"— skipping {option_symbol}"
        )
        return None

    # ── Gate 3: SL cooldown — block re-entry after stop loss ─────────────────
    if await redis_client.exists(f"sl:cooldown:{symbol}"):
        ttl = await redis_client.ttl(f"sl:cooldown:{symbol}")
        logger.info(f"[GATE] SL cooldown active for {symbol} ({ttl}s remaining) — skipping")
        return None

    existing = await redis_client.hget("positions:open", symbol)
    if existing:
        logger.info(f"Position already open for {symbol}, skipping")
        return None

    max_value = await get_max_position_value(redis_client)

    if not option_symbol:
        # No affordable option was found (get_affordable_option returned None) — skip trade
        logger.info(f"[GATE] No option symbol for {symbol} — skipping trade")
        return None

    if not option_price:
        logger.info(f"[GATE] No option price for {option_symbol} — skipping trade")
        return None

    # We always BUY to open (CE for bullish signal, PE for bearish) — slippage is always "BUY"
    lot_size = option_lot_size or 1
    entry_price = _apply_slippage(option_price, "BUY")
    cost_per_lot = entry_price * lot_size
    if cost_per_lot > max_value:
        logger.warning(
            f"[GATE] Option {option_symbol} costs ₹{cost_per_lot:.0f}/lot "
            f"(₹{entry_price:.2f} × {lot_size}) exceeds max position value "
            f"₹{max_value:.0f} — skipping trade"
        )
        return None
    num_lots = int(max_value / cost_per_lot)
    quantity = num_lots * lot_size
    trade_symbol = option_symbol
    raw_price = option_price

    trade_value = entry_price * quantity
    commission = _calculate_commission(trade_value)

    if not await allocate(redis_client, trade_value, fee=commission):
        return None

    # Override underlying-based SL/target with option-premium-relative levels.
    # The LLM decision produces index levels (e.g. SL=23850, target=24200) which are
    # meaningless for an option position whose entry is ₹150 — the option's own price
    # is the only valid reference. Levels must mirror the exit_rules constants so
    # Slack notifications show the actual thresholds that will trigger an exit.
    first_target_pct = RANGING_MILESTONE_PCT if day_type == "RANGING" else FIRST_MILESTONE_PCT
    stop_loss = round(entry_price * (1.0 - PREMIUM_SL_PCT), 2)   # e.g. entry × 0.90
    target    = round(entry_price * (1.0 + first_target_pct), 2) # e.g. entry × 1.20 (trending)

    trade_id = str(uuid.uuid4())
    now = datetime.now(IST)

    position = Position(
        symbol=symbol,
        side=side,
        quantity=quantity,
        avg_price=entry_price,
        entry_time=now,
        stop_loss=stop_loss,
        target=target,
        decision_id=decision_id,
        option_symbol=option_symbol,
        option_strike=option_strike,
        option_type=option_type,
        option_expiry=option_expiry,
        entry_option_price=option_price or 0.0,
        day_type=day_type,
    )
    # Capture entry IV from Redis if already populated by fast position watcher
    if option_symbol:
        try:
            greeks_raw = await redis_client.get(f"greeks:{option_symbol}")
            if greeks_raw:
                g = json.loads(greeks_raw)
                position.entry_iv = float(g.get("iv", 0) or 0)
        except Exception:
            pass

    trade = Trade(
        trade_id=trade_id,
        symbol=trade_symbol,
        side=side,
        quantity=quantity,
        entry_price=entry_price,
        entry_time=now,
        commission=commission,
        slippage=abs(entry_price - raw_price) * quantity,
        status="OPEN",
        decision_id=decision_id,
        reasoning=reasoning,
        option_symbol=option_symbol,
        option_strike=option_strike,
        option_type=option_type,
        option_expiry=option_expiry,
    )

    # Persist to Redis (operational cache)
    await redis_client.hset("positions:open", symbol, position.model_dump_json())
    await redis_client.hset("trades:all", trade_id, trade.model_dump_json())
    # Index trade_id by underlying so close_position can look it up without
    # a symbol mismatch (trade.symbol is the option symbol, not the underlying)
    await redis_client.hset("trades:open_id", symbol, trade_id)
    await redis_client.zadd(
        "trades:history",
        {trade.model_dump_json(): now.timestamp()},
    )

    # Persist to TimescaleDB via data-service (durable storage)
    await data_client.persist_trade({
        "trade_id":      trade.trade_id,
        "symbol":        trade.symbol,
        "side":          trade.side,
        "quantity":      trade.quantity,
        "entry_price":   trade.entry_price,
        "entry_time":    trade.entry_time.isoformat(),
        "commission":    trade.commission,
        "slippage":      trade.slippage,
        "status":        trade.status,
        "decision_id":   trade.decision_id,
        "reasoning":     trade.reasoning,
        "trading_mode":  "simulation",
        "option_symbol": trade.option_symbol,
        "option_strike": trade.option_strike,
        "option_type":   trade.option_type,
        "option_expiry": trade.option_expiry,
    })

    # Mark the source decision as acted upon for traceability
    if decision_id:
        await data_client.mark_decision_acted(decision_id, trade_id)

    label = f"{option_symbol} (strike ₹{option_strike})" if option_symbol else trade_symbol
    logger.info(
        f"OPENED {side} {num_lots} lot(s) × {lot_size} = {quantity}x{label} @ ₹{entry_price:.2f} "
        f"(commission=₹{commission:.0f})"
    )

    notify_trade_opened(
        mode="simulation",
        symbol=symbol,
        side=side,
        entry_price=entry_price,
        quantity=quantity,
        stop_loss=stop_loss,
        target=target,
        entry_time=trade.entry_time,
        option_symbol=option_symbol,
        option_strike=option_strike,
        option_type=option_type,
        option_expiry=option_expiry,
        reasoning=reasoning,
        day_type=day_type,
    )
    return trade


async def close_position(
    redis_client: aioredis.Redis,
    symbol: str,
    exit_price: float,
    status: str = "CLOSED",
    exit_reason: Optional[str] = None,
) -> Optional[Trade]:
    """Close an open position at exit_price."""
    pos_data = await redis_client.hget("positions:open", symbol)
    if not pos_data:
        return None

    pos = Position(**json.loads(pos_data))
    now = datetime.now(IST)
    # We always sell to close (bought to open regardless of underlying direction) —
    # slippage is always "SELL" (we receive slightly less than mid-market on exit)
    exit_price_with_slip = _apply_slippage(exit_price, "SELL")

    trade_value = exit_price_with_slip * pos.quantity
    commission = _calculate_commission(trade_value)

    # PnL is always (exit − entry) × qty — we are always long the option,
    # regardless of whether the underlying signal was BUY (CE) or SELL (PE).
    gross_pnl = (exit_price_with_slip - pos.avg_price) * pos.quantity

    # net_pnl for budget: entry commission already deducted from cash via allocate()
    budget_pnl = gross_pnl - commission
    invested_amount = pos.avg_price * pos.quantity

    # Look up the open trade ID directly by underlying symbol (O(1), no mismatch)
    # trade.symbol stores the option symbol for option trades, so scanning trades:all
    # by t.symbol == underlying would never match — use the index instead.
    trade = None
    trade_id_raw = await redis_client.hget("trades:open_id", symbol)
    if trade_id_raw:
        tdata = await redis_client.hget("trades:all", trade_id_raw)
        if tdata:
            trade = Trade(**json.loads(tdata))
    await redis_client.hdel("trades:open_id", symbol)

    if trade:
        entry_commission = trade.commission  # already stored at open time
        # true net pnl = gross minus both entry and exit commissions
        true_net_pnl = gross_pnl - commission - entry_commission
        trade.exit_price = exit_price_with_slip
        trade.exit_time = now
        trade.pnl = round(true_net_pnl, 2)
        trade.pnl_pct = round(true_net_pnl / invested_amount * 100, 3)
        trade.commission += commission  # total = entry + exit
        trade.slippage += abs(exit_price_with_slip - exit_price) * pos.quantity
        trade.status = status
        trade.exit_reason = exit_reason

        # Update Redis
        await redis_client.hset("trades:all", trade.trade_id, trade.model_dump_json())
        await redis_client.zadd(
            "trades:history",
            {trade.model_dump_json(): now.timestamp()},
        )

        # Persist closed trade to TimescaleDB (upsert updates the existing record)
        await data_client.persist_trade({
            "trade_id":      trade.trade_id,
            "symbol":        trade.symbol,
            "side":          trade.side,
            "quantity":      trade.quantity,
            "entry_price":   trade.entry_price,
            "entry_time":    trade.entry_time.isoformat(),
            "exit_price":    trade.exit_price,
            "exit_time":     trade.exit_time.isoformat(),
            "pnl":           trade.pnl,
            "pnl_pct":       trade.pnl_pct,
            "commission":    trade.commission,
            "slippage":      trade.slippage,
            "status":        trade.status,
            "decision_id":   trade.decision_id,
            "reasoning":     trade.reasoning,
            "trading_mode":  "simulation",
            "option_symbol": trade.option_symbol,
            "option_strike": trade.option_strike,
            "option_type":   trade.option_type,
            "option_expiry": trade.option_expiry,
            "exit_reason":   trade.exit_reason,
        })

    await release(redis_client, invested_amount, budget_pnl)
    await redis_client.hdel("positions:open", symbol)
    await _record_pnl_snapshot(redis_client, budget_pnl)

    logger.info(
        f"CLOSED {pos.side} {pos.quantity}x{symbol} @ ₹{exit_price_with_slip:.2f} "
        f"P&L=₹{budget_pnl:+.2f} ({status})"
    )

    if trade:
        notify_trade_closed(
            mode="simulation",
            symbol=symbol,
            side=pos.side,
            entry_price=trade.entry_price,
            exit_price=exit_price_with_slip,
            quantity=pos.quantity,
            pnl=trade.pnl or 0.0,
            pnl_pct=trade.pnl_pct or 0.0,
            commission=trade.commission,
            exit_reason=exit_reason or status,
            entry_time=trade.entry_time,
            exit_time=trade.exit_time,
            option_symbol=trade.option_symbol,
            option_strike=trade.option_strike,
            option_type=trade.option_type,
        )
    return trade


async def _record_pnl_snapshot(redis_client: aioredis.Redis, realized_pnl_delta: float) -> None:
    now = datetime.now(IST)
    date_key = f"pnl:daily:{now.strftime('%Y-%m-%d')}"
    existing = await redis_client.get("pnl:realized:total")
    total_realized = float(existing or 0) + realized_pnl_delta
    await redis_client.set("pnl:realized:total", str(total_realized))
    snapshot = json.dumps({
        "timestamp": now.isoformat(),
        "cumulative_pnl": total_realized,
    })
    await redis_client.zadd(date_key, {snapshot: now.timestamp()})
    await redis_client.expire(date_key, 86400 * 30)
