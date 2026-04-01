"""
Simulation Engine - subscribes to core-engine decisions and executes mock trades.
"""

import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI

from analytics.pnl import compute_pnl_summary, get_all_trades, get_open_positions
from config import settings
from execution import mock_broker, live_broker
from models.schemas import Position
from portfolio.budget import initialize_budget, load_budget
import data_client

logging.basicConfig(
    stream=sys.stdout,
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

redis_client: aioredis.Redis = None
_consumer_task: asyncio.Task = None


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

    if decision == "BUY":
        existing = await redis_client.hget("positions:open", symbol)
        if existing:
            pos = Position(**json.loads(existing))
            if pos.side == "SELL":
                await broker.close_position(redis_client, symbol, current_price)

        await broker.open_position(
            redis_client, symbol, "BUY", current_price,
            stop_loss, target, decision_id, reasoning,
        )

    elif decision == "SELL":
        existing = await redis_client.hget("positions:open", symbol)
        if existing:
            pos = Position(**json.loads(existing))
            if pos.side == "BUY":
                await broker.close_position(redis_client, symbol, current_price)

        await broker.open_position(
            redis_client, symbol, "SELL", current_price,
            stop_loss, target, decision_id, reasoning,
        )

    elif decision == "HOLD":
        pass


async def _check_stop_targets() -> None:
    """Check all open positions against stop-loss and target levels."""
    positions_raw = await redis_client.hgetall("positions:open")
    if not positions_raw:
        return

    for symbol, pos_data in positions_raw.items():
        try:
            pos = Position(**json.loads(pos_data))
            market_raw = await redis_client.get(f"market:{symbol}")
            if not market_raw:
                continue
            market = json.loads(market_raw)
            ltp = market.get("ltp", 0)
            if not ltp:
                continue

            mode = await redis_client.get("trading:mode") or "simulation"
            broker = live_broker if mode == "live" else mock_broker

            if pos.side == "BUY":
                if ltp <= pos.stop_loss:
                    logger.info(f"STOP LOSS triggered for {symbol} @ ₹{ltp:.2f}")
                    await broker.close_position(redis_client, symbol, ltp, status="STOPPED")
                elif ltp >= pos.target:
                    logger.info(f"TARGET hit for {symbol} @ ₹{ltp:.2f}")
                    await broker.close_position(redis_client, symbol, ltp, status="CLOSED")
            else:  # SELL
                if ltp >= pos.stop_loss:
                    logger.info(f"STOP LOSS triggered for {symbol} @ ₹{ltp:.2f}")
                    await broker.close_position(redis_client, symbol, ltp, status="STOPPED")
                elif ltp <= pos.target:
                    logger.info(f"TARGET hit for {symbol} @ ₹{ltp:.2f}")
                    await broker.close_position(redis_client, symbol, ltp, status="CLOSED")
        except Exception as e:
            logger.exception(f"Error checking stop/target for {symbol}: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client, _consumer_task
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    await initialize_budget(redis_client)
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
