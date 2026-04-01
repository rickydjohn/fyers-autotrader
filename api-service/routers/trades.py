import json
from typing import Optional
from fastapi import APIRouter, Depends, Query
import redis.asyncio as aioredis

from dependencies import get_redis
from models.schemas import ApiResponse

router = APIRouter(prefix="/trades", tags=["Trades"])


@router.get("")
async def get_trades(
    symbol: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    redis_client: aioredis.Redis = Depends(get_redis),
):
    # Get from sorted set (latest first)
    raw_items = await redis_client.zrevrange("trades:history", offset, offset + limit - 1)
    trades = []
    for item in raw_items:
        try:
            trade = json.loads(item)
            if symbol and trade.get("symbol") != symbol:
                continue
            trades.append(trade)
        except Exception:
            pass

    total = await redis_client.zcard("trades:history")
    return ApiResponse.ok({"total": total, "trades": trades})
