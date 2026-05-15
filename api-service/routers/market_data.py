import json
import time
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
    """Market snapshot for the chart.

    Reads the scan-time `market:{symbol}` blob for the full snapshot
    (indicators, candles, news), then overlays the freshest LTP from
    `ltp:{symbol}` (WS-fed sub-second) when available. Without this overlay
    the chart's live-price display would lag the WS feed by up to the scan
    interval (60s). The `market:` snapshot blob also carries an `ltp` field
    that is whatever the scheduler wrote on the last scan; we replace it
    with the WS value if newer.
    """
    raw = await redis_client.get(f"market:{symbol}")
    if not raw:
        raise HTTPException(status_code=404, detail=f"No data for symbol: {symbol}")
    snapshot = json.loads(raw)

    # Overlay live LTP from the WS feed if a fresh tick is in Redis.
    # Treat ≤30s as fresh (matches the ltp:* TTL); fall through silently
    # otherwise so the snapshot's own ltp is returned unchanged.
    try:
        ltp_raw = await redis_client.get(f"ltp:{symbol}")
        if ltp_raw:
            ltp_data = json.loads(ltp_raw)
            ts_ms = int(ltp_data.get("ts") or 0)
            age_ms = int(time.time() * 1000) - ts_ms
            ltp_val = ltp_data.get("ltp")
            if ts_ms and age_ms <= 30_000 and ltp_val is not None:
                snapshot["ltp"] = float(ltp_val)
                snapshot["ltp_source"] = "ws"
                snapshot["ltp_age_ms"] = age_ms
    except Exception:
        # Live-LTP overlay is best-effort; never let it break the endpoint.
        pass

    return ApiResponse.ok(snapshot)


@router.get("/symbols")
async def list_symbols(redis_client: aioredis.Redis = Depends(get_redis)):
    """List all symbols with cached market data."""
    keys = await redis_client.keys("market:*")
    symbols = [k.replace("market:", "") for k in keys]
    return ApiResponse.ok({"symbols": symbols})
