"""Monthly and cumulative trade report endpoints."""
import calendar
from datetime import datetime, timedelta
from typing import Optional

import pytz
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from db.connection import get_db
from db.models import Trade
from repositories.trades import _trade_to_dict

router = APIRouter(prefix="/report", tags=["Report"])
IST = pytz.timezone("Asia/Kolkata")


@router.get("/trades")
async def monthly_trade_report(
    month: str = Query(..., description="Month in YYYY-MM format, e.g. 2026-04"),
    trading_mode: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Return all trades for a given month with summary stats and exit-reason breakdown."""
    year, mon = int(month[:4]), int(month[5:7])
    since = datetime(year, mon, 1, 0, 0, 0, tzinfo=IST)
    last_day = calendar.monthrange(year, mon)[1]
    until = datetime(year, mon, last_day, 23, 59, 59, tzinfo=IST)

    q = (
        select(Trade)
        .where(and_(Trade.entry_time >= since, Trade.entry_time <= until))
        .order_by(Trade.entry_time.asc())
    )
    if trading_mode:
        q = q.where(Trade.trading_mode == trading_mode)

    result = await db.execute(q)
    trades = [_trade_to_dict(r) for r in result.scalars().all()]

    closed = [t for t in trades if t["status"] != "OPEN"]
    winners = [t for t in closed if (t["pnl"] or 0) > 0]
    losers  = [t for t in closed if (t["pnl"] or 0) <= 0]
    net_pnl = sum(t["pnl"] or 0 for t in closed)

    # Breakdown by exit reason for pie chart
    reason_breakdown: dict = {}
    for t in closed:
        reason = t.get("exit_reason") or t["status"]
        if reason not in reason_breakdown:
            reason_breakdown[reason] = {"count": 0, "pnl": 0.0, "wins": 0, "losses": 0}
        rb = reason_breakdown[reason]
        rb["count"] += 1
        rb["pnl"] = round(rb["pnl"] + (t["pnl"] or 0), 2)
        if (t["pnl"] or 0) > 0:
            rb["wins"] += 1
        else:
            rb["losses"] += 1

    return {
        "month": month,
        "summary": {
            "total_trades": len(closed),
            "winners": len(winners),
            "losers": len(losers),
            "win_rate": round(len(winners) / len(closed) * 100, 1) if closed else 0.0,
            "net_pnl": round(net_pnl, 2),
            "gross_profit": round(sum(t["pnl"] or 0 for t in winners), 2),
            "gross_loss": round(sum(t["pnl"] or 0 for t in losers), 2),
        },
        "by_exit_reason": reason_breakdown,
        "trades": trades,
    }


def _build_summary_and_breakdown(trades: list) -> dict:
    closed = [t for t in trades if t["status"] != "OPEN"]
    winners = [t for t in closed if (t["pnl"] or 0) > 0]
    losers  = [t for t in closed if (t["pnl"] or 0) <= 0]
    net_pnl = sum(t["pnl"] or 0 for t in closed)

    reason_breakdown: dict = {}
    for t in closed:
        reason = t.get("exit_reason") or t["status"]
        if reason not in reason_breakdown:
            reason_breakdown[reason] = {"count": 0, "pnl": 0.0, "wins": 0, "losses": 0}
        rb = reason_breakdown[reason]
        rb["count"] += 1
        rb["pnl"] = round(rb["pnl"] + (t["pnl"] or 0), 2)
        if (t["pnl"] or 0) > 0:
            rb["wins"] += 1
        else:
            rb["losses"] += 1

    by_month: dict = {}
    for t in closed:
        entry_time = t.get("entry_time") or ""
        month_key = str(entry_time)[:7]
        if not month_key or len(month_key) < 7:
            continue
        if month_key not in by_month:
            by_month[month_key] = {"total_trades": 0, "winners": 0, "losers": 0, "net_pnl": 0.0, "win_rate": 0.0}
        bm = by_month[month_key]
        bm["total_trades"] += 1
        pnl = t["pnl"] or 0
        bm["net_pnl"] = round(bm["net_pnl"] + pnl, 2)
        if pnl > 0:
            bm["winners"] += 1
        else:
            bm["losers"] += 1
    for bm in by_month.values():
        bm["win_rate"] = round(bm["winners"] / bm["total_trades"] * 100, 1) if bm["total_trades"] else 0.0

    return {
        "summary": {
            "total_trades": len(closed),
            "winners": len(winners),
            "losers": len(losers),
            "win_rate": round(len(winners) / len(closed) * 100, 1) if closed else 0.0,
            "net_pnl": round(net_pnl, 2),
            "gross_profit": round(sum(t["pnl"] or 0 for t in winners), 2),
            "gross_loss": round(sum(t["pnl"] or 0 for t in losers), 2),
        },
        "by_exit_reason": reason_breakdown,
        "by_month": by_month,
        "trades": trades,
    }


@router.get("/cumulative")
async def cumulative_trade_report(
    from_date: Optional[str] = Query(None, description="Start date YYYY-MM-DD, defaults to 365 days ago"),
    to_date: Optional[str] = Query(None, description="End date YYYY-MM-DD, defaults to today"),
    trading_mode: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    today = datetime.now(IST).date()
    end   = datetime.strptime(to_date,   "%Y-%m-%d").date() if to_date   else today
    start = datetime.strptime(from_date, "%Y-%m-%d").date() if from_date else (end - timedelta(days=365))

    since = datetime(start.year, start.month, start.day, 0,  0,  0,  tzinfo=IST)
    until = datetime(end.year,   end.month,   end.day,   23, 59, 59, tzinfo=IST)

    q = (
        select(Trade)
        .where(and_(Trade.entry_time >= since, Trade.entry_time <= until))
        .order_by(Trade.entry_time.asc())
    )
    if trading_mode:
        q = q.where(Trade.trading_mode == trading_mode)

    result = await db.execute(q)
    trades = [_trade_to_dict(r) for r in result.scalars().all()]

    data = _build_summary_and_breakdown(trades)
    return {"from_date": start.isoformat(), "to_date": end.isoformat(), **data}
