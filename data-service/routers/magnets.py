"""
Price magnet zones: unfilled gaps and unbreached CPR zones.

  GET /magnets/{symbol} — return both gap and CPR magnets for a symbol
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from db.connection import get_db
from repositories.market_data import get_unfilled_gaps, get_unfilled_cprs

router = APIRouter(tags=["Magnet Zones"])


@router.get("/magnets/{symbol:path}")
async def fetch_magnet_zones(
    symbol: str,
    min_gap_pts: float = Query(100.0, description="Minimum gap size in points"),
    gap_lookback_days: int = Query(90, description="How far back to scan for gaps"),
    max_cpr_age_td: int = Query(22, description="Max unbreached CPR age in trading days"),
    cpr_lookback_days: int = Query(60, description="How far back to scan for CPR zones"),
    db: AsyncSession = Depends(get_db),
):
    """Return unfilled gap zones and unbreached CPR zones for a symbol."""
    gaps = await get_unfilled_gaps(
        db, symbol,
        min_gap_pts=min_gap_pts,
        lookback_days=gap_lookback_days,
    )
    cprs = await get_unfilled_cprs(
        db, symbol,
        max_age_trading_days=max_cpr_age_td,
        lookback_days=cpr_lookback_days,
    )
    return {
        "status": "ok",
        "symbol": symbol,
        "gaps":  [dict(g) for g in gaps],
        "cprs":  [dict(c) for c in cprs],
    }
