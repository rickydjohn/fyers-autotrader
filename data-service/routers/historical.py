"""
Historical data query router.
GET /historical-data?symbol=NSE:NIFTY50-INDEX&interval=5m&limit=200
"""

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from db.connection import get_db
from repositories.market_data import get_candles

router = APIRouter(tags=["Historical Data"])

VALID_INTERVALS = {"1m", "5m", "15m", "1h", "daily"}


@router.get("/historical-data")
async def get_historical_data(
    symbol:   str = Query(..., example="NSE:NIFTY50-INDEX"),
    interval: str = Query("5m",  regex="^(1m|5m|15m|1h|daily)$"),
    limit:    int = Query(200,  ge=1, le=1000),
    since:    Optional[str] = Query(None, description="ISO datetime string"),
    db: AsyncSession = Depends(get_db),
):
    since_dt: Optional[datetime] = None
    if since:
        since_dt = datetime.fromisoformat(since)

    candles = await get_candles(db, symbol, interval=interval, limit=limit, since=since_dt)
    return {
        "status": "ok",
        "symbol": symbol,
        "interval": interval,
        "count": len(candles),
        "candles": candles,
    }
