"""
Volume profile API — historical average 5m volume per time slot per symbol.
"""

import urllib.parse
from pydantic import BaseModel
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from db.connection import get_db
from repositories.market_data import get_volume_profile, update_volume_profile_for_date

router = APIRouter()


@router.get("/volume-profile/{symbol:path}")
async def volume_profile_endpoint(symbol: str, db: AsyncSession = Depends(get_db)):
    decoded = urllib.parse.unquote(symbol)
    slots = await get_volume_profile(db, decoded)
    return {"symbol": decoded, "slots": slots}


class VolumeProfileUpdateIn(BaseModel):
    symbol: str
    session_date: str  # ISO date YYYY-MM-DD


@router.post("/volume-profile/update")
async def update_volume_profile_endpoint(payload: VolumeProfileUpdateIn, db: AsyncSession = Depends(get_db)):
    await update_volume_profile_for_date(db, payload.symbol, payload.session_date)
    return {"status": "ok", "symbol": payload.symbol, "session_date": payload.session_date}
