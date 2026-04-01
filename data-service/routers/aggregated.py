"""
Aggregated view router — delegates to the same get_candles logic
(continuous aggregates are transparent in the DB layer).
GET /aggregated-view?symbol=...&interval=1h
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from db.connection import get_db
from repositories.market_data import get_candles, get_recent_daily_indicators

router = APIRouter(tags=["Aggregated Views"])


@router.get("/aggregated-view")
async def get_aggregated_view(
    symbol:   str = Query(..., example="NSE:NIFTY50-INDEX"),
    interval: str = Query("1h", regex="^(5m|15m|1h|daily)$"),
    limit:    int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    candles = await get_candles(db, symbol, interval=interval, limit=limit)
    return {
        "status":   "ok",
        "symbol":   symbol,
        "interval": interval,
        "count":    len(candles),
        "candles":  candles,
    }


@router.get("/daily-indicators")
async def get_daily_indicators(
    symbol: str = Query(..., example="NSE:NIFTY50-INDEX"),
    days:   int = Query(5, ge=1, le=30),
    db: AsyncSession = Depends(get_db),
):
    rows = await get_recent_daily_indicators(db, symbol, days=days)
    return {"status": "ok", "symbol": symbol, "days": days, "data": rows}
