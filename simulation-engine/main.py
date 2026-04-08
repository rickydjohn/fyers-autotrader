"""
Simulation Engine - subscribes to core-engine decisions and executes mock trades.
"""

import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager

from datetime import datetime

import pytz
import redis.asyncio as aioredis
from fastapi import FastAPI

from analytics.pnl import compute_pnl_summary, get_all_trades, get_open_positions
from config import settings
from execution import mock_broker, live_broker
from execution.exit_rules import check_exit
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

    option_symbol = data.get("option_symbol") or None
    option_type = data.get("option_type") or None
    option_expiry = data.get("option_expiry") or None
    try:
        option_strike = int(float(data.get("option_strike", 0) or 0)) or None
        option_price = float(data.get("option_price", 0) or 0) or None
        option_lot_size = int(float(data.get("option_lot_size", 0) or 0)) or None
    except (ValueError, TypeError):
        option_strike = option_price = option_lot_size = None

    # Determine day type from CPR width embedded in the decision's indicators snapshot
    day_type: str = "TRENDING"
    try:
        ind_raw = data.get("indicators") or "{}"
        ind_dict = json.loads(ind_raw) if isinstance(ind_raw, str) else ind_raw
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

    if decision == "BUY":
        existing = await redis_client.hget("positions:open", symbol)
        if existing:
            pos = Position(**json.loads(existing))
            if pos.side == "SELL":
                await broker.close_position(redis_client, symbol, current_price)

        await broker.open_position(
            redis_client, symbol, "BUY", current_price,
            stop_loss, target, decision_id, reasoning,
            option_symbol=option_symbol, option_strike=option_strike,
            option_type=option_type, option_expiry=option_expiry,
            option_price=option_price, option_lot_size=option_lot_size,
            day_type=day_type,
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
            option_symbol=option_symbol, option_strike=option_strike,
            option_type=option_type, option_expiry=option_expiry,
            option_price=option_price, option_lot_size=option_lot_size,
            day_type=day_type,
        )

    elif decision == "HOLD":
        pass


async def _check_stop_targets() -> None:
    """
    Evaluate exit conditions for all open positions.
    Runs on every consumer loop tick (~5s).  Prices are kept fresh by the
    fast_position_watcher in core-engine (every POSITION_WATCHER_INTERVAL_SECONDS).
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

            # Underlying LTP — prefer the fast-watcher key (ltp:{symbol}, 30s TTL)
            # which is refreshed every POSITION_WATCHER_INTERVAL_SECONDS; fall back
            # to the full market snapshot written by the slower scan job.
            ltp_raw = await redis_client.get(f"ltp:{symbol}") or await redis_client.get(f"market:{symbol}")
            if not ltp_raw:
                continue
            underlying_ltp = json.loads(ltp_raw).get("ltp", 0)
            if not underlying_ltp:
                continue

            # Option LTP and Greeks (populated by fast_position_watcher)
            option_ltp: float | None = None
            greeks: dict | None = None
            if pos.option_symbol:
                opt_raw = await redis_client.get(f"market:{pos.option_symbol}")
                if opt_raw:
                    option_ltp = json.loads(opt_raw).get("ltp")
                greeks_raw = await redis_client.get(f"greeks:{pos.option_symbol}")
                if greeks_raw:
                    greeks = json.loads(greeks_raw)

            # Index indicators for milestone confirmation (from full market snapshot)
            indicators: dict = {}
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

            # Track peak option price (update pos in memory; write-back deferred below)
            peak_updated = False
            if option_ltp and option_ltp > pos.peak_option_price:
                pos.peak_option_price = option_ltp
                peak_updated = True

            should_exit, reason, exit_price, new_milestone = check_exit(
                pos, underlying_ltp, option_ltp, greeks, indicators, now
            )

            if should_exit:
                # Map detailed reason to the CLOSED/STOPPED DB status enum
                db_status = "CLOSED" if reason == "CLOSED" else "STOPPED"
                await broker.close_position(
                    redis_client, symbol, exit_price,
                    status=db_status, exit_reason=reason,
                )
                # Block re-entry after a stop loss to prevent chasing
                if reason == "STOP_LOSS":
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client, _consumer_task
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    await initialize_budget(redis_client)
    await reconcile_invested(redis_client)
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
