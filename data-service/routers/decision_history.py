"""
Decision history router.
GET /decision-history
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from db.connection import get_db
from repositories.decisions import get_decisions
from repositories.trades import get_trades, get_pnl_summary

router = APIRouter(tags=["Decision History"])


@router.get("/decision-history")
async def get_decision_history(
    symbol:        Optional[str] = Query(None),
    limit:         int           = Query(100, ge=1, le=500),
    since:         Optional[str] = Query(None, description="ISO datetime"),
    decision_type: Optional[str] = Query(None, regex="^(BUY|SELL|HOLD)$"),
    db: AsyncSession = Depends(get_db),
):
    since_dt = datetime.fromisoformat(since) if since else None
    decisions = await get_decisions(db, symbol=symbol, limit=limit, since=since_dt, decision_type=decision_type)
    return {"status": "ok", "count": len(decisions), "decisions": decisions}


@router.get("/trade-history")
async def get_trade_history(
    symbol: Optional[str] = Query(None),
    status: Optional[str] = Query(None, regex="^(OPEN|CLOSED|STOPPED)$"),
    limit:  int           = Query(100, ge=1, le=500),
    since:  Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    since_dt = datetime.fromisoformat(since) if since else None
    trades = await get_trades(db, symbol=symbol, status=status, limit=limit, since=since_dt)
    return {"status": "ok", "count": len(trades), "trades": trades}


@router.get("/pnl-summary")
async def get_pnl_summary_endpoint(
    since: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    since_dt = datetime.fromisoformat(since) if since else None
    summary = await get_pnl_summary(db, since=since_dt)
    return {"status": "ok", **summary}
