"""
API Service — unified REST + SSE gateway.
Reads from Redis, proxies to core/sim/data engines where needed.

v2: Added historical data and context proxy routes.
"""

import logging
import sys
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from routers import decision_log, market_data, pnl, positions, trades
from routers.historical import router as historical_router
from routers.trading_mode import router as trading_mode_router

logging.basicConfig(
    stream=sys.stdout,
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    app.state.http_client = httpx.AsyncClient(
        base_url=settings.data_service_url,
        timeout=10.0,
    )
    app.state.http_core_client = httpx.AsyncClient(
        base_url=settings.core_engine_url,
        timeout=10.0,
    )
    logger.info("API service started")
    yield
    await app.state.http_client.aclose()
    await app.state.http_core_client.aclose()
    await app.state.redis.aclose()
    logger.info("API service shutdown")


app = FastAPI(
    title="Trading Intelligence API",
    version="2.0.0",
    description="REST + SSE API for the trading intelligence system",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PREFIX = "/api/v1"
app.include_router(market_data.router,  prefix=PREFIX)
app.include_router(trades.router,       prefix=PREFIX)
app.include_router(positions.router,    prefix=PREFIX)
app.include_router(pnl.router,          prefix=PREFIX)
app.include_router(decision_log.router, prefix=PREFIX)
app.include_router(historical_router,   prefix=PREFIX)
app.include_router(trading_mode_router, prefix=PREFIX)


@app.get("/healthz")
async def health():
    redis_status = "error"
    try:
        await app.state.redis.ping()
        redis_status = "ok"
    except Exception:
        pass

    data_service_status = "error"
    try:
        r = await app.state.http_client.get("/healthz", timeout=2.0)
        data_service_status = "ok" if r.status_code == 200 else "degraded"
    except Exception:
        data_service_status = "unavailable"

    return {
        "status": "ok" if redis_status == "ok" else "degraded",
        "checks": {
            "redis": redis_status,
            "data_service": data_service_status,
        },
    }


@app.get("/")
async def root():
    return {
        "service": "Trading Intelligence API",
        "docs": "/docs",
        "health": "/healthz",
    }
