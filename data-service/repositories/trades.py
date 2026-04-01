"""
Repository: trade records.
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

import pytz

from db.models import Trade

IST = pytz.timezone("Asia/Kolkata")


async def upsert_trade(db: AsyncSession, trade: Dict[str, Any]) -> None:
    stmt = insert(Trade).values(**trade)
    stmt = stmt.on_conflict_do_update(
        index_elements=["trade_id"],
        set_={k: v for k, v in trade.items() if k != "trade_id"},
    )
    await db.execute(stmt)
    await db.commit()


async def get_trades(
    db: AsyncSession,
    symbol: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
    since: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    q = select(Trade).order_by(Trade.entry_time.desc()).limit(limit)
    if symbol:
        q = q.where(Trade.symbol == symbol)
    if status:
        q = q.where(Trade.status == status.upper())
    if since:
        q = q.where(Trade.entry_time >= since)
    result = await db.execute(q)
    rows = result.scalars().all()
    return [_trade_to_dict(r) for r in rows]


async def get_pnl_summary(
    db: AsyncSession,
    since: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Aggregate closed trade P&L from DB."""
    cutoff = since or (datetime.now(IST) - timedelta(days=30))
    result = await db.execute(
        select(Trade)
        .where(Trade.status.in_(["CLOSED", "STOPPED"]), Trade.exit_time >= cutoff)
        .order_by(Trade.exit_time.asc())
    )
    rows = result.scalars().all()
    total_pnl = sum(float(r.pnl or 0) for r in rows)
    wins = [r for r in rows if (r.pnl or 0) > 0]
    losses = [r for r in rows if (r.pnl or 0) <= 0]
    return {
        "total_trades": len(rows),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "total_pnl": round(total_pnl, 2),
        "win_rate": round(len(wins) / len(rows) * 100, 1) if rows else 0.0,
        "avg_win": round(sum(float(r.pnl or 0) for r in wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(float(r.pnl or 0) for r in losses) / len(losses), 2) if losses else 0.0,
        "trades": [_trade_to_dict(r) for r in rows[-20:]],
    }


def _trade_to_dict(row: Trade) -> Dict[str, Any]:
    return {
        "trade_id":    row.trade_id,
        "symbol":      row.symbol,
        "side":        row.side,
        "quantity":    row.quantity,
        "entry_price": float(row.entry_price),
        "entry_time":  row.entry_time.isoformat(),
        "exit_price":  float(row.exit_price) if row.exit_price is not None else None,
        "exit_time":   row.exit_time.isoformat() if row.exit_time else None,
        "pnl":         float(row.pnl) if row.pnl is not None else None,
        "pnl_pct":     float(row.pnl_pct) if row.pnl_pct is not None else None,
        "commission":  float(row.commission),
        "slippage":    float(row.slippage),
        "status":      row.status,
        "decision_id": row.decision_id,
        "reasoning":   row.reasoning,
    }
