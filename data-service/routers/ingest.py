"""
Ingest router — receives data from core-engine and simulation-engine.
POST /ingest/candle
POST /ingest/daily-indicator
POST /ingest/decision
POST /ingest/trade
POST /ingest/news
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from db.connection import get_db
from repositories.market_data import upsert_candle, upsert_daily_indicator, insert_options_oi_batch
from repositories.decisions import upsert_decision
from repositories.trades import upsert_trade
from repositories.news import insert_news_batch

router = APIRouter(prefix="/ingest", tags=["Ingest"])


class CandleIn(BaseModel):
    time: datetime
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    vwap: Optional[float] = None
    rsi: Optional[float] = None
    ema_9: Optional[float] = None
    ema_21: Optional[float] = None


class DailyIndicatorIn(BaseModel):
    date: str          # ISO date string
    symbol: str
    prev_high: float
    prev_low: float
    prev_close: float
    pivot: float
    bc: float
    tc: float
    r1: float
    r2: float
    r3: float
    s1: float
    s2: float
    s3: float
    cpr_width_pct: float


class DecisionIn(BaseModel):
    decision_id: str
    time: datetime
    symbol: str
    decision: str
    confidence: float
    reasoning: str
    stop_loss: float = 0.0
    target: float = 0.0
    risk_reward: float = 0.0
    indicators_snapshot: Optional[Dict[str, Any]] = None
    acted_upon: bool = False
    trade_id: Optional[str] = None
    historical_context: Optional[Dict[str, Any]] = None


class TradeIn(BaseModel):
    trade_id: str
    symbol: str
    side: str
    quantity: int
    entry_price: float
    entry_time: datetime
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
    commission: float = 0.0
    slippage: float = 0.0
    status: str = "OPEN"
    decision_id: Optional[str] = None
    reasoning: Optional[str] = None
    trading_mode: str = "simulation"
    option_symbol: Optional[str] = None
    option_strike: Optional[int] = None
    option_type: Optional[str] = None
    option_expiry: Optional[str] = None
    exit_reason: Optional[str] = None
    broker_order_id: Optional[str] = None


class NewsItemIn(BaseModel):
    time: datetime
    title: str
    summary: Optional[str] = None
    source: str
    sentiment_score: float = 0.0


@router.post("/candle")
async def ingest_candle(payload: CandleIn, db: AsyncSession = Depends(get_db)):
    await upsert_candle(db, payload.model_dump())
    return {"status": "ok"}


@router.post("/candles")
async def ingest_candles(payload: List[CandleIn], db: AsyncSession = Depends(get_db)):
    for candle in payload:
        await upsert_candle(db, candle.model_dump())
    return {"status": "ok", "count": len(payload)}


@router.post("/daily-indicator")
async def ingest_daily_indicator(payload: DailyIndicatorIn, db: AsyncSession = Depends(get_db)):
    data = payload.model_dump()
    from datetime import date
    data["date"] = date.fromisoformat(data["date"])
    await upsert_daily_indicator(db, data)
    return {"status": "ok"}


@router.post("/decision")
async def ingest_decision(payload: DecisionIn, db: AsyncSession = Depends(get_db)):
    await upsert_decision(db, payload.model_dump())
    return {"status": "ok"}


@router.post("/trade")
async def ingest_trade(payload: TradeIn, db: AsyncSession = Depends(get_db)):
    await upsert_trade(db, payload.model_dump())
    return {"status": "ok"}


@router.patch("/decision/{decision_id}/acted")
async def mark_decision_acted(
    decision_id: str,
    trade_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Mark a decision as acted upon and link the resulting trade_id."""
    from sqlalchemy import text
    result = await db.execute(
        text(
            "UPDATE ai_decisions SET acted_upon = true, trade_id = :trade_id "
            "WHERE decision_id = :decision_id"
        ),
        {"decision_id": decision_id, "trade_id": trade_id},
    )
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="decision_id not found")
    return {"status": "ok", "decision_id": decision_id, "trade_id": trade_id}


class OptionsOiIn(BaseModel):
    time: datetime
    symbol: str
    expiry: str          # ISO date YYYY-MM-DD
    strike: int
    option_type: str     # CE or PE
    ltp: Optional[float] = None
    oi: Optional[int] = None
    oi_change: Optional[int] = None
    volume: Optional[int] = None


@router.post("/options-oi")
async def ingest_options_oi(payload: List[OptionsOiIn], db: AsyncSession = Depends(get_db)):
    rows = [r.model_dump() for r in payload]
    count = await insert_options_oi_batch(db, rows)
    return {"status": "ok", "inserted": count}


@router.post("/news")
async def ingest_news(payload: List[NewsItemIn], db: AsyncSession = Depends(get_db)):
    items = [n.model_dump() for n in payload]
    count = await insert_news_batch(db, items)
    return {"status": "ok", "inserted": count}
