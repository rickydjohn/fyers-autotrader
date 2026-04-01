import json
from fastapi import APIRouter, Depends, HTTPException, Query
import redis.asyncio as aioredis

from dependencies import get_redis
from models.schemas import ApiResponse

router = APIRouter(prefix="/market-data", tags=["Market Data"])


@router.get("")
async def get_market_data(
    symbol: str = Query(..., example="NSE:NIFTY50-INDEX"),
    redis_client: aioredis.Redis = Depends(get_redis),
):
    data = await redis_client.get(f"market:{symbol}")
    if not data:
        raise HTTPException(status_code=404, detail=f"No data for symbol: {symbol}")
    return ApiResponse.ok(json.loads(data))


@router.get("/symbols")
async def list_symbols(redis_client: aioredis.Redis = Depends(get_redis)):
    """List all symbols with cached market data."""
    keys = await redis_client.keys("market:*")
    symbols = [k.replace("market:", "") for k in keys]
    return ApiResponse.ok({"symbols": symbols})
