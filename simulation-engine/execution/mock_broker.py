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
import data_client

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
) -> Optional[Trade]:
    """Open a new simulated position. Trades the option if one is provided."""
    existing = await redis_client.hget("positions:open", symbol)
    if existing:
        logger.info(f"Position already open for {symbol}, skipping")
        return None

    max_value = await get_max_position_value(redis_client)

    if option_symbol and option_price:
        # Trade the option: entry_price = option premium; quantity = 1 lot (from Fyers depth)
        lot_size = option_lot_size or 1
        entry_price = _apply_slippage(option_price, side)
        quantity = lot_size
        trade_symbol = option_symbol
        raw_price = option_price
    else:
        # Fallback: trade the underlying directly
        entry_price = _apply_slippage(price, side)
        quantity = _calculate_quantity(entry_price, max_value)
        trade_symbol = symbol
        raw_price = price

    trade_value = entry_price * quantity
    commission = _calculate_commission(trade_value)
    total_cost = trade_value + commission

    if not await allocate(redis_client, total_cost):
        return None

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
    )

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
        f"OPENED {side} {quantity}x{label} @ ₹{entry_price:.2f} "
        f"(commission=₹{commission:.0f})"
    )
    return trade


async def close_position(
    redis_client: aioredis.Redis,
    symbol: str,
    exit_price: float,
    status: str = "CLOSED",
) -> Optional[Trade]:
    """Close an open position at exit_price."""
    pos_data = await redis_client.hget("positions:open", symbol)
    if not pos_data:
        return None

    pos = Position(**json.loads(pos_data))
    now = datetime.now(IST)
    exit_price_with_slip = _apply_slippage(
        exit_price, "SELL" if pos.side == "BUY" else "BUY"
    )

    trade_value = exit_price_with_slip * pos.quantity
    commission = _calculate_commission(trade_value)

    if pos.side == "BUY":
        gross_pnl = (exit_price_with_slip - pos.avg_price) * pos.quantity
    else:
        gross_pnl = (pos.avg_price - exit_price_with_slip) * pos.quantity

    net_pnl = gross_pnl - commission
    invested_amount = pos.avg_price * pos.quantity

    # Find the open trade in Redis
    all_trades_raw = await redis_client.hgetall("trades:all")
    trade = None
    for tid, tdata in all_trades_raw.items():
        t = Trade(**json.loads(tdata))
        if t.symbol == symbol and t.status == "OPEN":
            trade = t
            break

    if trade:
        trade.exit_price = exit_price_with_slip
        trade.exit_time = now
        trade.pnl = round(net_pnl, 2)
        trade.pnl_pct = round(net_pnl / invested_amount * 100, 3)
        trade.commission += commission
        trade.slippage += abs(exit_price_with_slip - exit_price) * pos.quantity
        trade.status = status

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
        })

    await release(redis_client, invested_amount, net_pnl)
    await redis_client.hdel("positions:open", symbol)
    await _record_pnl_snapshot(redis_client, net_pnl)

    logger.info(
        f"CLOSED {pos.side} {pos.quantity}x{symbol} @ ₹{exit_price_with_slip:.2f} "
        f"P&L=₹{net_pnl:+.2f} ({status})"
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
