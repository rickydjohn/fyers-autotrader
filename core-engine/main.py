"""
Core Engine - FastAPI entrypoint.
Handles Fyers OAuth, market data ingestion, indicators, LLM decisions.

v2: Bootstraps historical context from data-service at startup.
"""

# Force IPv4 for all outbound connections so Fyers API traffic routes through
# the IPv4 proxy rather than bypassing it via the container's IPv6 address.
import socket as _socket
_orig_getaddrinfo = _socket.getaddrinfo
def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, _socket.AF_INET, type, proto, flags)
_socket.getaddrinfo = _ipv4_only_getaddrinfo

import logging
import sys
from contextlib import asynccontextmanager
from typing import Optional

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from config import settings
from fyers.proxy import configure_fyers_proxy
from fyers.auth import exchange_auth_code, get_auth_url, get_valid_token
from fyers.orders import get_funds, get_fyers_positions, get_order_fill, place_market_order
from llm.client import check_ollama_health
from scheduler.jobs import (
    create_scheduler, _refresh_news, run_market_scan,
    refresh_context_cache, bootstrap_historical_data,
    bootstrap_daily_ohlcv, _load_sr_cache, bootstrap_magnet_zones,
)
import data_client

logging.basicConfig(
    stream=sys.stdout,
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

# Apply proxy patch before any Fyers SDK calls are made
configure_fyers_proxy()

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
        # 1. Intraday multi-timeframe candles (market_candles, 90-day retention)
        for symbol in settings.symbols:
            try:
                await bootstrap_historical_data(symbol, redis_client)
            except Exception as e:
                logger.warning(f"Historical bootstrap failed for {symbol}: {e}")

        # 2. Multi-year daily OHLCV → S/R level computation (permanent storage)
        for symbol in settings.symbols:
            try:
                result = await bootstrap_daily_ohlcv(symbol, redis_client, years=5)
                logger.info(
                    f"Daily bootstrap {symbol}: "
                    f"{result['daily_bars']} bars, {result['sr_levels']} S/R levels"
                )
            except Exception as e:
                logger.warning(f"Daily OHLCV bootstrap failed for {symbol}: {e}")

        # 3. Load SR cache if bootstrap didn't populate it (e.g. re-deploy on existing cluster)
        for symbol in settings.symbols:
            try:
                await _load_sr_cache(symbol, redis_client)
            except Exception as e:
                logger.warning(f"SR cache load failed for {symbol}: {e}")

        # 4. Refresh multi-timeframe context snapshots
        for symbol in settings.symbols:
            try:
                await refresh_context_cache(symbol)
            except Exception as e:
                logger.warning(f"Context bootstrap failed for {symbol}: {e}")

        # 5. Bootstrap magnet zones (unfilled gaps + unbreached CPRs) — check Redis first
        for symbol in settings.symbols:
            try:
                await bootstrap_magnet_zones(symbol, redis_client)
            except Exception as e:
                logger.warning(f"Magnet zone bootstrap failed for {symbol}: {e}")

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


@app.get("/fyers/positions")
async def fyers_positions_endpoint():
    """Return currently open positions from the Fyers account."""
    try:
        positions = get_fyers_positions()
        if positions is None:
            raise HTTPException(status_code=503, detail="Could not fetch positions — is Fyers authenticated?")
        return {"status": "ok", "positions": positions}
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


@app.get("/fyers/orders/{order_id}/status")
async def fyers_order_status(order_id: str):
    """Poll Fyers for the fill status of a placed order. Returns traded price when filled."""
    try:
        result = get_order_fill(order_id, max_attempts=1, interval_s=0)
        if result is None:
            raise HTTPException(status_code=503, detail="Could not reach Fyers orderbook")
        return {"status": "ok", "order": result}
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.post("/scan/trigger")
async def trigger_scan():
    await run_market_scan(redis_client)
    return {"status": "ok", "message": "Scan triggered"}


@app.post("/historical/backfill")
async def historical_backfill(symbols: Optional[str] = Query(None)):
    """
    Pull multi-timeframe OHLC from Fyers into Timescale (via data-service).
    Defaults to NIFTY50 and BANK NIFTY index symbols from config.
    Pass `symbols` as comma-separated Fyers symbols, e.g. `NSE:NIFTY50-INDEX,NSE:NIFTYBANK-INDEX`.
    """
    if symbols:
        syms = [s.strip() for s in symbols.split(",") if s.strip()]
    else:
        syms = list(settings.symbols)
    if not syms:
        raise HTTPException(status_code=400, detail="No symbols to backfill")

    results = []
    for sym in syms:
        try:
            summary = await bootstrap_historical_data(sym, redis_client)
            results.append(summary)
        except Exception as e:
            logger.exception(f"Backfill failed for {sym}: {e}")
            results.append({"symbol": sym, "error": str(e)})
    for sym in syms:
        try:
            await refresh_context_cache(sym)
        except Exception as e:
            logger.warning(f"Context refresh after backfill failed for {sym}: {e}")
    return {"status": "ok", "results": results}


@app.get("/options/chain/latest")
async def get_options_chain_latest(symbol: str = "NSE:NIFTY50-INDEX"):
    """Return the most recent options chain OI snapshot from Redis."""
    data = await redis_client.get(f"options:chain:{symbol}")
    if not data:
        raise HTTPException(status_code=404, detail="No options chain snapshot yet — wait for next 5-min interval")
    import json as _json
    return {"status": "ok", "data": _json.loads(data)}


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
