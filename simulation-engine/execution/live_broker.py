"""
Live broker: executes real Fyers orders for live trading mode.
Mirrors mock_broker's Redis + data-service tracking so the UI and P&L
work identically regardless of trading mode.
"""
import json
import logging
import uuid
from datetime import datetime
from typing import Optional

import httpx
import pytz
import redis.asyncio as aioredis

from config import settings
from models.schemas import Position, Trade
import data_client

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

CORE_URL = settings.core_engine_url


async def _place_fyers_order(symbol: str, side: str, quantity: int) -> Optional[dict]:
    """Call core-engine to place a live Fyers market order."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            r = await client.post(
                f"{CORE_URL}/fyers/orders/place",
                params={"symbol": symbol, "side": side, "quantity": quantity},
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"Live order request failed ({symbol} {side} {quantity}): {e}")
            return None


async def _get_available_funds() -> float:
    """Fetch available equity balance from Fyers via core-engine."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            r = await client.get(f"{CORE_URL}/fyers/funds")
            r.raise_for_status()
            funds = r.json().get("funds", {})
            # Fyers returns keys like "available_balance", "net_available", etc.
            for key in ("available_balance", "net_available", "available_margin", "total_balance"):
                if key in funds:
                    return float(funds[key])
            return 0.0
        except Exception as e:
            logger.error(f"Could not fetch Fyers funds: {e}")
            return 0.0


async def open_position(
    redis_client: aioredis.Redis,
    symbol: str,
    side: str,
    price: float,
    stop_loss: float,
    target: float,
    decision_id: str,
    reasoning: str,
) -> Optional[Trade]:
    """Place a live Fyers order and record the position in Redis + data-service."""
    existing = await redis_client.hget("positions:open", symbol)
    if existing:
        logger.info(f"Live position already open for {symbol}, skipping")
        return None

    available = await _get_available_funds()
    max_value = available * (settings.max_position_size_pct / 100)
    if max_value <= 0 or price <= 0:
        logger.warning(f"Insufficient funds (₹{available:.0f}) or invalid price for {symbol}")
        return None

    quantity = max(1, int(max_value / price))
    order_result = await _place_fyers_order(symbol, side, quantity)
    if order_result is None:
        return None

    trade_id = str(uuid.uuid4())
    now = datetime.now(IST)

    position = Position(
        symbol=symbol,
        side=side,
        quantity=quantity,
        avg_price=price,
        entry_time=now,
        stop_loss=stop_loss,
        target=target,
        decision_id=decision_id,
    )
    trade = Trade(
        trade_id=trade_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        entry_price=price,
        entry_time=now,
        commission=20.0,   # STT + brokerage approximation
        slippage=0.0,
        status="OPEN",
        decision_id=decision_id,
        reasoning=reasoning,
    )

    await redis_client.hset("positions:open", symbol, position.model_dump_json())
    await redis_client.hset("trades:all", trade_id, trade.model_dump_json())
    await redis_client.zadd("trades:history", {trade.model_dump_json(): now.timestamp()})

    await data_client.persist_trade({
        "trade_id":    trade.trade_id,
        "symbol":      trade.symbol,
        "side":        trade.side,
        "quantity":    trade.quantity,
        "entry_price": trade.entry_price,
        "entry_time":  trade.entry_time.isoformat(),
        "commission":  trade.commission,
        "slippage":    trade.slippage,
        "status":      trade.status,
        "decision_id": trade.decision_id,
        "reasoning":   trade.reasoning,
    })

    logger.info(f"LIVE OPENED {side} {quantity}x{symbol} @ ₹{price:.2f} (order_id={order_result.get('order_id')})")
    return trade


async def close_position(
    redis_client: aioredis.Redis,
    symbol: str,
    exit_price: float,
    status: str = "CLOSED",
) -> Optional[Trade]:
    """Close a live position by placing the reverse order, then update records."""
    pos_data = await redis_client.hget("positions:open", symbol)
    if not pos_data:
        return None

    pos = Position(**json.loads(pos_data))
    close_side = "SELL" if pos.side == "BUY" else "BUY"
    await _place_fyers_order(symbol, close_side, pos.quantity)

    now = datetime.now(IST)
    commission = 20.0
    if pos.side == "BUY":
        gross_pnl = (exit_price - pos.avg_price) * pos.quantity
    else:
        gross_pnl = (pos.avg_price - exit_price) * pos.quantity
    net_pnl = gross_pnl - commission
    invested = pos.avg_price * pos.quantity

    all_trades_raw = await redis_client.hgetall("trades:all")
    trade = None
    for tid, tdata in all_trades_raw.items():
        t = Trade(**json.loads(tdata))
        if t.symbol == symbol and t.status == "OPEN":
            trade = t
            break

    if trade:
        trade.exit_price = exit_price
        trade.exit_time = now
        trade.pnl = round(net_pnl, 2)
        trade.pnl_pct = round(net_pnl / invested * 100, 3) if invested else 0.0
        trade.commission += commission
        trade.status = status

        await redis_client.hset("trades:all", trade.trade_id, trade.model_dump_json())
        await redis_client.zadd("trades:history", {trade.model_dump_json(): now.timestamp()})

        await data_client.persist_trade({
            "trade_id":    trade.trade_id,
            "symbol":      trade.symbol,
            "side":        trade.side,
            "quantity":    trade.quantity,
            "entry_price": trade.entry_price,
            "entry_time":  trade.entry_time.isoformat(),
            "exit_price":  trade.exit_price,
            "exit_time":   trade.exit_time.isoformat(),
            "pnl":         trade.pnl,
            "pnl_pct":     trade.pnl_pct,
            "commission":  trade.commission,
            "slippage":    trade.slippage,
            "status":      trade.status,
            "decision_id": trade.decision_id,
            "reasoning":   trade.reasoning,
        })

    await redis_client.hdel("positions:open", symbol)
    existing_total = float(await redis_client.get("pnl:realized:total") or 0)
    await redis_client.set("pnl:realized:total", str(existing_total + net_pnl))

    logger.info(f"LIVE CLOSED {pos.side} {pos.quantity}x{symbol} @ ₹{exit_price:.2f} P&L=₹{net_pnl:+.2f} ({status})")
    return trade
