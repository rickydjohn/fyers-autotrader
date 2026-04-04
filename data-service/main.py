"""
Data Service — persistent storage, historical queries, context builder.
Port 8003.
"""

import json
import logging
import sys
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from config import settings
from db.connection import engine
from routers import ingest, historical, aggregated, context, decision_history
from routers.context import set_redis_client
from context.builder import build_context_snapshot, format_context_for_prompt
from db.connection import AsyncSessionLocal

logging.basicConfig(
    stream=sys.stdout,
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Lightweight schema guard for existing DB volumes.
    async with engine.begin() as conn:
        await conn.execute(text(
            "ALTER TABLE IF EXISTS trades "
            "ADD COLUMN IF NOT EXISTS trading_mode TEXT NOT NULL DEFAULT 'simulation'"
        ))
        await conn.execute(text(
            "DO $$ "
            "BEGIN "
            "  IF to_regclass('public.trades') IS NOT NULL THEN "
            "    CREATE INDEX IF NOT EXISTS trades_mode_entry_time_idx "
            "    ON trades (trading_mode, entry_time DESC); "
            "  END IF; "
            "END $$;"
        ))

    # Connect Redis
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    set_redis_client(redis_client)
    logger.info("Redis connected")

    # Bootstrap context snapshots for each watched symbol into Redis
    symbols = ["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX"]
    async with AsyncSessionLocal() as db:
        for symbol in symbols:
            try:
                ctx = await build_context_snapshot(db, symbol)
                await redis_client.setex(
                    f"context:{symbol}",
                    300,
                    json.dumps(ctx, default=str),
                )
                logger.info(f"Context snapshot bootstrapped for {symbol}")
            except Exception as e:
                logger.warning(f"Could not bootstrap context for {symbol}: {e}")

    yield

    await redis_client.aclose()
    await engine.dispose()
    logger.info("Data service shutdown complete")


app = FastAPI(
    title="Trading Data Service",
    version="1.0.0",
    description="Persistent storage, historical queries, multi-timeframe context.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

PREFIX = "/api/v1"
app.include_router(ingest.router,           prefix=PREFIX)
app.include_router(historical.router,       prefix=PREFIX)
app.include_router(aggregated.router,       prefix=PREFIX)
app.include_router(context.router,          prefix=PREFIX)
app.include_router(decision_history.router, prefix=PREFIX)


@app.get("/healthz")
async def health():
    try:
        async with engine.connect() as conn:
            await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
        db_status = "ok"
    except Exception:
        db_status = "error"
    return {
        "status": "ok" if db_status == "ok" else "degraded",
        "checks": {"timescaledb": db_status},
    }


@app.get("/")
async def root():
    return {
        "service": "Trading Data Service",
        "docs": "/docs",
        "health": "/healthz",
    }
