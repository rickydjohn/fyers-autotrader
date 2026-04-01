"""
Context snapshot router.
GET /context-snapshot?symbol=NSE:NIFTY50-INDEX
"""

import json
from typing import Optional

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db.connection import get_db
from context.builder import build_context_snapshot, format_context_for_prompt

router = APIRouter(tags=["Context"])

_redis: Optional[aioredis.Redis] = None


def get_redis() -> aioredis.Redis:
    return _redis


def set_redis_client(client: aioredis.Redis) -> None:
    global _redis
    _redis = client


@router.get("/context-snapshot")
async def get_context_snapshot(
    symbol: str = Query(..., example="NSE:NIFTY50-INDEX"),
    fresh:  bool = Query(False, description="Bypass Redis cache and rebuild from DB"),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    cache_key = f"context:{symbol}"

    if not fresh and redis:
        cached = await redis.get(cache_key)
        if cached:
            return {"status": "ok", "source": "cache", "context": json.loads(cached)}

    ctx = await build_context_snapshot(db, symbol, lookback_days=settings.context_lookback_days)

    # Cache for 5 minutes — context doesn't need to be fresher than that
    if redis:
        await redis.setex(cache_key, 300, json.dumps(ctx, default=str))

    return {"status": "ok", "source": "db", "context": ctx}


@router.get("/context-snapshot/prompt")
async def get_context_as_prompt(
    symbol: str = Query(..., example="NSE:NIFTY50-INDEX"),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    """Return the formatted markdown block ready for injection into an Ollama prompt."""
    cache_key = f"context:{symbol}"
    ctx = None
    if redis:
        cached = await redis.get(cache_key)
        if cached:
            ctx = json.loads(cached)
    if ctx is None:
        ctx = await build_context_snapshot(db, symbol, lookback_days=settings.context_lookback_days)
    prompt_block = format_context_for_prompt(ctx)
    return {"status": "ok", "symbol": symbol, "prompt_block": prompt_block}
