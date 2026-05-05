"""
Live broker: executes real Fyers orders for live trading mode.
Mirrors mock_broker's Redis + data-service tracking so the UI and P&L
work identically regardless of trading mode.
"""
import asyncio
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
from notifications.slack import notify_trade_opened, notify_trade_closed
import data_client
from execution.exit_rules import PREMIUM_SL_PCT, FIRST_MILESTONE_PCT, RANGING_MILESTONE_PCT

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

CORE_URL = settings.core_engine_url


async def _place_fyers_order(symbol: str, side: str, quantity: int) -> Optional[dict]:
    """Call core-engine to place a live Fyers market order. Returns {"order_id": str, ...}."""
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


async def _await_fill(order_id: str, max_attempts: int = 10, interval_s: float = 1.0) -> Optional[dict]:
    """
    Poll core-engine for order fill status, returning {"traded_price": float, "filled_qty": int}
    when the order is confirmed as TRADED, or None if rejected/cancelled/timeout.
    Each poll is a single check (no blocking sleep inside core-engine); we sleep here between calls.
    """
    async with httpx.AsyncClient(timeout=5.0) as client:
        for attempt in range(1, max_attempts + 1):
            try:
                r = await client.get(f"{CORE_URL}/fyers/orders/{order_id}/status")
                r.raise_for_status()
                order = r.json().get("order", {})
                status = order.get("status")
                if status == "TRADED":
                    return order
                if status in ("REJECTED", "CANCELLED"):
                    logger.error(f"Order {order_id} {status} — aborting")
                    return None
                logger.debug(f"Order {order_id} status={status} (attempt {attempt}/{max_attempts})")
            except Exception as e:
                logger.warning(f"Fill poll failed for {order_id} (attempt {attempt}): {e}")
            if attempt < max_attempts:
                await asyncio.sleep(interval_s)

    logger.warning(f"Order {order_id} fill not confirmed after {max_attempts}s — using decision price")
    return {"status": "TIMEOUT", "traded_price": 0.0, "filled_qty": 0}


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
    option_symbol: Optional[str] = None,
    option_strike: Optional[int] = None,
    option_type: Optional[str] = None,
    option_expiry: Optional[str] = None,
    option_price: Optional[float] = None,
    option_lot_size: Optional[int] = None,
    day_type: Optional[str] = None,
    dte: int = 99,
) -> Optional[Trade]:
    """Place a live Fyers order and record the position in Redis + data-service."""
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

    pending_key = f"pending:order:{symbol}"
    if await redis_client.exists(pending_key) or await redis_client.hget("positions:open", symbol):
        logger.info(f"Live position already open (or order pending) for {symbol}, skipping")
        return None

    available = await _get_available_funds()
    max_value = available * (settings.max_position_size_pct / 100)
    if max_value <= 0 or price <= 0:
        logger.warning(f"Insufficient funds (₹{available:.0f}) or invalid price for {symbol}")
        return None

    # Gate: live mode only trades options contracts — never place orders on the raw index.
    # If option selection returned None (chain fetch failed, budget exhausted, etc.) abort here.
    if option_symbol is None:
        logger.warning(
            f"[GATE] No option symbol resolved for {symbol} ({side}) — "
            f"skipping trade to avoid invalid index order"
        )
        return None

    if option_price is None or option_price <= 0:
        logger.warning(
            f"[GATE] Option {option_symbol} has no valid price — skipping trade"
        )
        return None

    lot_size = option_lot_size or 1
    total_option_cost = option_price * lot_size
    # Reserve 5% buffer for brokerage, STT, exchange charges and premium slippage
    # between quote-time and order-fill. Without this, Fyers rejects with
    # margin shortfall even when the quoted premium appears affordable.
    spendable = available * 0.95
    if total_option_cost > spendable:
        logger.warning(
            f"[GATE] Option {option_symbol} costs ₹{total_option_cost:.0f} "
            f"(₹{option_price:.2f} × {lot_size} lots) exceeds spendable funds "
            f"₹{spendable:.0f} (95% of ₹{available:.0f}) — skipping trade"
        )
        return None
    trade_price = option_price
    trade_symbol = option_symbol
    quantity = lot_size

    await redis_client.set(pending_key, "1", ex=30)
    # We are always option buyers — the Fyers entry order is always BUY.
    # The `side` field records directional intent (BUY=CE, SELL=PE) for position tracking.
    order_result = await _place_fyers_order(trade_symbol, "BUY", quantity)
    if order_result is None:
        await redis_client.delete(pending_key)
        return None

    broker_order_id = order_result.get("order_id")

    # Poll for actual fill confirmation and true fill price
    fill = await _await_fill(broker_order_id) if broker_order_id else None
    if fill is None:
        # Fyers rejected or cancelled the order — abort
        logger.error(f"Entry order {broker_order_id} was not filled — aborting position for {trade_symbol}")
        await redis_client.delete(pending_key)
        return None

    # Use actual fill price if available; fall back to decision price on timeout
    actual_entry_price = fill.get("traded_price") or trade_price
    if actual_entry_price <= 0:
        actual_entry_price = trade_price

    # Override underlying-based SL/target with option-premium-relative levels.
    # The LLM decision produces index levels which are meaningless for an option
    # position — the option's own price is the only valid reference.
    if option_symbol:
        first_target_pct = RANGING_MILESTONE_PCT if day_type == "RANGING" else FIRST_MILESTONE_PCT
        stop_loss = round(actual_entry_price * (1.0 - PREMIUM_SL_PCT), 2)
        target    = round(actual_entry_price * (1.0 + first_target_pct), 2)

    trade_id = str(uuid.uuid4())
    now = datetime.now(IST)

    position = Position(
        symbol=symbol,
        side=side,
        quantity=quantity,
        avg_price=actual_entry_price,
        entry_time=now,
        stop_loss=stop_loss,
        target=target,
        decision_id=decision_id,
        option_symbol=option_symbol,
        option_strike=option_strike,
        option_type=option_type,
        option_expiry=option_expiry,
        entry_option_price=actual_entry_price,
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
        entry_price=actual_entry_price,
        entry_time=now,
        commission=20.0,
        slippage=0.0,
        status="OPEN",
        decision_id=decision_id,
        reasoning=reasoning,
        option_symbol=option_symbol,
        option_strike=option_strike,
        option_type=option_type,
        option_expiry=option_expiry,
        broker_order_id=broker_order_id,
    )

    await redis_client.hset("positions:open", symbol, position.model_dump_json())
    await redis_client.delete(pending_key)  # position is now in Redis; pending flag no longer needed
    await redis_client.hset("trades:all", trade_id, trade.model_dump_json())
    await redis_client.hset("trades:open_id", symbol, trade_id)
    await redis_client.zadd("trades:history", {trade.model_dump_json(): now.timestamp()})

    await data_client.persist_trade({
        "trade_id":        trade.trade_id,
        "symbol":          trade.symbol,
        "side":            trade.side,
        "quantity":        trade.quantity,
        "entry_price":     trade.entry_price,
        "entry_time":      trade.entry_time.isoformat(),
        "commission":      trade.commission,
        "slippage":        trade.slippage,
        "status":          trade.status,
        "decision_id":     trade.decision_id,
        "reasoning":       trade.reasoning,
        "trading_mode":    "live",
        "option_symbol":   trade.option_symbol,
        "option_strike":   trade.option_strike,
        "option_type":     trade.option_type,
        "option_expiry":   trade.option_expiry,
        "broker_order_id": trade.broker_order_id,
    })

    label = f"{option_symbol} (strike ₹{option_strike})" if option_symbol else trade_symbol
    fill_note = f"filled=₹{actual_entry_price:.2f}" if fill.get("status") == "TRADED" else "price=decision (timeout)"
    logger.info(f"LIVE OPENED {side} {quantity}x{label} | {fill_note} | order_id={broker_order_id}")

    notify_trade_opened(
        mode="live",
        symbol=symbol,
        side=side,
        entry_price=actual_entry_price,
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
    """Close a live position by placing the reverse order, then update records."""
    pos_data = await redis_client.hget("positions:open", symbol)
    if not pos_data:
        return None

    pos = Position(**json.loads(pos_data))
    exit_symbol = pos.option_symbol or symbol

    # We always bought to open, so we always sell to close.
    exit_order_result = await _place_fyers_order(exit_symbol, "SELL", pos.quantity)

    # If the SELL order failed, do NOT clear the position from Redis.
    # The exit watcher runs every 5s and will re-trigger close_position on the
    # next cycle, giving the order another attempt (proxy hiccup, transient error).
    # Fyers auto-squares intraday positions at EOD so the risk is bounded.
    if exit_order_result is None:
        logger.error(
            f"[EXIT FAILED] SELL order for {exit_symbol} rejected by Fyers — "
            f"position kept open in Redis, will retry on next watcher cycle."
        )
        return None

    # Poll for actual exit fill price from Fyers
    actual_exit_price = exit_price  # fallback: price from exit_rules (e.g. last LTP)
    if exit_order_result:
        exit_order_id = exit_order_result.get("order_id")
        if exit_order_id:
            fill = await _await_fill(exit_order_id)
            if fill and fill.get("status") == "TRADED" and fill.get("traded_price", 0) > 0:
                actual_exit_price = fill["traded_price"]
                logger.info(f"Exit fill confirmed: {exit_order_id} @ ₹{actual_exit_price:.2f}")
            else:
                logger.warning(
                    f"Exit fill not confirmed for {exit_order_id} — using LTP ₹{exit_price:.2f}"
                )

    now = datetime.now(IST)
    commission = 20.0
    # We are always long the option (bought to open regardless of underlying direction).
    # For non-option equity trades, direction still determines PnL sign.
    if pos.option_symbol or pos.side == "BUY":
        gross_pnl = (actual_exit_price - pos.avg_price) * pos.quantity
    else:
        gross_pnl = (pos.avg_price - actual_exit_price) * pos.quantity
    net_pnl = gross_pnl - commission
    invested = pos.avg_price * pos.quantity

    trade = None
    trade_id_raw = await redis_client.hget("trades:open_id", symbol)
    if trade_id_raw:
        tdata = await redis_client.hget("trades:all", trade_id_raw)
        if tdata:
            trade = Trade(**json.loads(tdata))
    await redis_client.hdel("trades:open_id", symbol)

    if trade:
        trade.exit_price = actual_exit_price
        trade.exit_time = now
        trade.pnl = round(net_pnl, 2)
        trade.pnl_pct = round(net_pnl / invested * 100, 3) if invested else 0.0
        trade.commission += commission
        trade.status = status
        trade.exit_reason = exit_reason

        await redis_client.hset("trades:all", trade.trade_id, trade.model_dump_json())
        await redis_client.zadd("trades:history", {trade.model_dump_json(): now.timestamp()})

        await data_client.persist_trade({
            "trade_id":        trade.trade_id,
            "symbol":          trade.symbol,
            "side":            trade.side,
            "quantity":        trade.quantity,
            "entry_price":     trade.entry_price,
            "entry_time":      trade.entry_time.isoformat(),
            "exit_price":      trade.exit_price,
            "exit_time":       trade.exit_time.isoformat(),
            "pnl":             trade.pnl,
            "pnl_pct":         trade.pnl_pct,
            "commission":      trade.commission,
            "slippage":        trade.slippage,
            "status":          trade.status,
            "decision_id":     trade.decision_id,
            "reasoning":       trade.reasoning,
            "trading_mode":    "live",
            "option_symbol":   trade.option_symbol,
            "option_strike":   trade.option_strike,
            "option_type":     trade.option_type,
            "option_expiry":   trade.option_expiry,
            "exit_reason":     trade.exit_reason,
            "broker_order_id": trade.broker_order_id,
        })

    await redis_client.hdel("positions:open", symbol)
    existing_total = float(await redis_client.get("pnl:realized:total") or 0)
    await redis_client.set("pnl:realized:total", str(existing_total + net_pnl))

    logger.info(
        f"LIVE CLOSED {pos.side} {pos.quantity}x{symbol} "
        f"@ ₹{actual_exit_price:.2f} P&L=₹{net_pnl:+.2f} ({status})"
    )

    if trade:
        notify_trade_closed(
            mode="live",
            symbol=symbol,
            side=pos.side,
            entry_price=trade.entry_price,
            exit_price=actual_exit_price,
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


async def record_external_close(
    redis_client: aioredis.Redis,
    symbol: str,
    exit_price: float,
    exit_reason: str = "USER_EXIT_FYERS",
) -> Optional[Trade]:
    """
    Record a position as closed without placing a new Fyers order.
    Used when the position was already closed externally — by the user on the
    Fyers platform — and the system just needs to reconcile its state.
    """
    pos_data = await redis_client.hget("positions:open", symbol)
    if not pos_data:
        return None

    pos = Position(**json.loads(pos_data))
    now = datetime.now(IST)
    commission = 20.0
    # Always long the option regardless of underlying direction
    if pos.option_symbol or pos.side == "BUY":
        gross_pnl = (exit_price - pos.avg_price) * pos.quantity
    else:
        gross_pnl = (pos.avg_price - exit_price) * pos.quantity
    net_pnl = gross_pnl - commission
    invested = pos.avg_price * pos.quantity

    trade = None
    trade_id_raw = await redis_client.hget("trades:open_id", symbol)
    if trade_id_raw:
        tdata = await redis_client.hget("trades:all", trade_id_raw)
        if tdata:
            trade = Trade(**json.loads(tdata))
    await redis_client.hdel("trades:open_id", symbol)

    if trade:
        trade.exit_price = exit_price
        trade.exit_time = now
        trade.pnl = round(net_pnl, 2)
        trade.pnl_pct = round(net_pnl / invested * 100, 3) if invested else 0.0
        trade.commission += commission
        trade.status = "CLOSED"
        trade.exit_reason = exit_reason

        await redis_client.hset("trades:all", trade.trade_id, trade.model_dump_json())
        await redis_client.zadd("trades:history", {trade.model_dump_json(): now.timestamp()})

        await data_client.persist_trade({
            "trade_id":        trade.trade_id,
            "symbol":          trade.symbol,
            "side":            trade.side,
            "quantity":        trade.quantity,
            "entry_price":     trade.entry_price,
            "entry_time":      trade.entry_time.isoformat(),
            "exit_price":      trade.exit_price,
            "exit_time":       trade.exit_time.isoformat(),
            "pnl":             trade.pnl,
            "pnl_pct":         trade.pnl_pct,
            "commission":      trade.commission,
            "slippage":        trade.slippage,
            "status":          trade.status,
            "decision_id":     trade.decision_id,
            "reasoning":       trade.reasoning,
            "trading_mode":    "live",
            "option_symbol":   trade.option_symbol,
            "option_strike":   trade.option_strike,
            "option_type":     trade.option_type,
            "option_expiry":   trade.option_expiry,
            "exit_reason":     trade.exit_reason,
            "broker_order_id": trade.broker_order_id,
        })

    await redis_client.hdel("positions:open", symbol)
    existing_total = float(await redis_client.get("pnl:realized:total") or 0)
    await redis_client.set("pnl:realized:total", str(existing_total + net_pnl))

    logger.info(
        f"EXTERNAL CLOSE recorded: {pos.side} {pos.quantity}x{symbol} "
        f"@ ₹{exit_price:.2f} P&L=₹{net_pnl:+.2f} reason={exit_reason}"
    )

    if trade:
        notify_trade_closed(
            mode="live",
            symbol=symbol,
            side=pos.side,
            entry_price=trade.entry_price,
            exit_price=exit_price,
            quantity=pos.quantity,
            pnl=trade.pnl or 0.0,
            pnl_pct=trade.pnl_pct or 0.0,
            commission=trade.commission,
            exit_reason=exit_reason,
            entry_time=trade.entry_time,
            exit_time=trade.exit_time,
            option_symbol=trade.option_symbol,
            option_strike=trade.option_strike,
            option_type=trade.option_type,
        )
    return trade
