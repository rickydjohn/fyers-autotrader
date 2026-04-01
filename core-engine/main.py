"""
Core Engine - FastAPI entrypoint.
Handles Fyers OAuth, market data ingestion, indicators, LLM decisions.

v2: Bootstraps historical context from data-service at startup.
"""

import logging
import sys
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from config import settings
from fyers.auth import exchange_auth_code, get_auth_url, get_valid_token
from fyers.orders import get_funds, place_market_order
from llm.client import check_ollama_health
from scheduler.jobs import create_scheduler, _refresh_news, run_market_scan, refresh_context_cache, bootstrap_historical_data
import data_client

logging.basicConfig(
    stream=sys.stdout,
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

redis_client: aioredis.Redis = None
scheduler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client, scheduler
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    logger.info("Redis connected")

    scheduler = create_scheduler(redis_client)
    scheduler.start()
    logger.info("Scheduler started")

    # Warm up news cache immediately
    try:
        await _refresh_news(redis_client)
    except Exception as e:
        logger.warning(f"Initial news fetch failed: {e}")

    # Bootstrap historical candles from Fyers for all symbols (non-blocking)
    import asyncio
    async def _do_bootstrap():
        for symbol in settings.symbols:
            try:
                await bootstrap_historical_data(symbol, redis_client)
            except Exception as e:
                logger.warning(f"Historical bootstrap failed for {symbol}: {e}")
        # After candles are persisted, refresh multi-timeframe context
        for symbol in settings.symbols:
            try:
                await refresh_context_cache(symbol)
            except Exception as e:
                logger.warning(f"Context bootstrap failed for {symbol}: {e}")
    asyncio.create_task(_do_bootstrap())

    yield

    scheduler.shutdown(wait=False)
    await data_client.close_client()
    await redis_client.aclose()
    logger.info("Core engine shutdown complete")


app = FastAPI(
    title="Trading Core Engine",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def health_check():
    checks = {}
    try:
        await redis_client.ping()
        checks["redis"] = "ok"
    except Exception:
        checks["redis"] = "error"

    ollama_ok = await check_ollama_health()
    checks["ollama"] = "ok" if ollama_ok else "unavailable"

    try:
        get_valid_token()
        checks["fyers_auth"] = "ok"
    except RuntimeError:
        checks["fyers_auth"] = "not_authenticated"

    # Check data-service connectivity
    try:
        import httpx
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(f"{settings.data_service_url}/healthz")
            checks["data_service"] = "ok" if r.status_code == 200 else "degraded"
    except Exception:
        checks["data_service"] = "unavailable"

    status = "ok" if checks["redis"] == "ok" else "degraded"
    return {"status": status, "checks": checks}


@app.get("/fyers/auth")
async def fyers_auth():
    url = get_auth_url()
    return RedirectResponse(url=url)


@app.get("/fyers/callback")
async def fyers_callback(auth_code: str = Query(...)):
    try:
        token = exchange_auth_code(auth_code)
        return {
            "status": "ok",
            "message": "Authentication successful. Token saved.",
            "token_preview": f"{token[:8]}...",
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/trading-mode")
async def get_trading_mode():
    mode = await redis_client.get("trading:mode") or "simulation"
    return {"mode": mode}


@app.post("/trading-mode")
async def set_trading_mode(mode: str = Query(...)):
    if mode not in ("simulation", "live"):
        raise HTTPException(status_code=400, detail="mode must be 'simulation' or 'live'")
    await redis_client.set("trading:mode", mode)
    logger.info(f"Trading mode set to: {mode}")
    return {"mode": mode}


@app.get("/fyers/funds")
async def fyers_funds():
    try:
        funds = get_funds()
        if funds is None:
            raise HTTPException(status_code=503, detail="Could not fetch funds — is Fyers authenticated?")
        return {"status": "ok", "funds": funds}
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.post("/fyers/orders/place")
async def fyers_place_order(
    symbol: str = Query(...),
    side: str = Query(...),
    quantity: int = Query(...),
):
    if side not in ("BUY", "SELL"):
        raise HTTPException(status_code=400, detail="side must be BUY or SELL")
    if quantity <= 0:
        raise HTTPException(status_code=400, detail="quantity must be > 0")
    try:
        result = place_market_order(symbol, side, quantity)
        if result is None:
            raise HTTPException(status_code=500, detail="Order placement failed")
        return {"status": "ok", "order_id": result.get("id"), "data": result}
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.post("/scan/trigger")
async def trigger_scan():
    await run_market_scan(redis_client)
    return {"status": "ok", "message": "Scan triggered"}


@app.get("/market/{symbol:path}")
async def get_market_snapshot(symbol: str):
    data = await redis_client.get(f"market:{symbol}")
    if not data:
        raise HTTPException(status_code=404, detail=f"No data for {symbol}")
    return {"status": "ok", "data": data}


@app.get("/context/{symbol:path}")
async def get_context(symbol: str):
    """Return the in-memory historical context for a symbol."""
    from scheduler.jobs import _context_cache
    ctx = _context_cache.get(symbol)
    if not ctx:
        raise HTTPException(status_code=404, detail=f"No context for {symbol}")
    return {"status": "ok", "symbol": symbol, "context": ctx}
