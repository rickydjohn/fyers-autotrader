"""
Historical S/R level routes.

  POST /ingest/daily-ohlcv        — batch upsert multi-year daily bars
  POST /ingest/sr-levels          — replace computed S/R levels for a symbol
  GET  /sr-levels                 — fetch levels (optionally near a price)
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from db.connection import get_db
from repositories.sr_levels import (
    get_sr_levels,
    replace_sr_levels,
    upsert_daily_ohlcv_batch,
)

router = APIRouter(tags=["Historical S/R"])


# ── Pydantic schemas ───────────────────────────────────────────────────────────

class DailyBarIn(BaseModel):
    date:   str     # ISO date  YYYY-MM-DD
    symbol: str
    open:   float
    high:   float
    low:    float
    close:  float
    volume: int = 0


class SRLevelIn(BaseModel):
    level:      float
    level_type: str          # SUPPORT | RESISTANCE | BOTH
    strength:   int = 1
    first_seen: Optional[str] = None   # ISO date
    last_seen:  Optional[str] = None   # ISO date


class SRLevelBatchIn(BaseModel):
    symbol: str
    levels: List[SRLevelIn]


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("/ingest/daily-ohlcv")
async def ingest_daily_ohlcv(
    payload: List[DailyBarIn],
    db: AsyncSession = Depends(get_db),
):
    """Batch-upsert daily OHLCV bars into the permanent daily_ohlcv table."""
    from datetime import date as _date
    rows = [
        {
            "date":   _date.fromisoformat(b.date),
            "symbol": b.symbol,
            "open":   b.open,
            "high":   b.high,
            "low":    b.low,
            "close":  b.close,
            "volume": b.volume,
        }
        for b in payload
    ]
    count = await upsert_daily_ohlcv_batch(db, rows)
    return {"status": "ok", "upserted": count}


@router.post("/ingest/sr-levels")
async def ingest_sr_levels(
    payload: SRLevelBatchIn,
    db: AsyncSession = Depends(get_db),
):
    """Replace the computed S/R level set for a symbol (atomic swap)."""
    from datetime import date as _date
    levels = [
        {
            "level":      l.level,
            "level_type": l.level_type,
            "strength":   l.strength,
            "first_seen": _date.fromisoformat(l.first_seen) if l.first_seen else None,
            "last_seen":  _date.fromisoformat(l.last_seen)  if l.last_seen  else None,
        }
        for l in payload.levels
    ]
    count = await replace_sr_levels(db, payload.symbol, levels)
    return {"status": "ok", "symbol": payload.symbol, "levels": count}


@router.get("/sr-levels")
async def fetch_sr_levels(
    symbol:     str            = Query(..., example="NSE:NIFTY50-INDEX"),
    near_price: Optional[float] = Query(None, description="Filter to levels within ±pct_band% of this price"),
    pct_band:   float           = Query(10.0, description="Band width % around near_price"),
    limit:      int             = Query(25,   ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Fetch historical S/R levels, optionally filtered by proximity to a price."""
    levels = await get_sr_levels(db, symbol, near_price=near_price, pct_band=pct_band, limit=limit)
    return {
        "status": "ok",
        "symbol": symbol,
        "count":  len(levels),
        "levels": levels,
    }
