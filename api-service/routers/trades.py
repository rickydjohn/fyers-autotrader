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
    # Read from trades:all (hash) — always holds the latest version of each trade,
    # deduplicated by trade_id. trades:history (sorted set) stores both OPEN and
    # CLOSED versions as separate members and is only used for ordering.
    raw_all = await redis_client.hgetall("trades:all")

    trades = []
    for item in raw_all.values():
        try:
            trade = json.loads(item)
            if symbol and trade.get("symbol") != symbol:
                continue
            trades.append(trade)
        except Exception:
            pass

    # Sort by entry_time descending (newest first)
    trades.sort(key=lambda t: t.get("entry_time", ""), reverse=True)

    total = len(trades)
    page = trades[offset: offset + limit]
    return ApiResponse.ok({"total": total, "trades": page})
