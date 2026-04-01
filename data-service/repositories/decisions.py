"""
Repository: AI decision log.
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

import pytz

from db.models import AiDecision

IST = pytz.timezone("Asia/Kolkata")


async def upsert_decision(db: AsyncSession, decision: Dict[str, Any]) -> None:
    stmt = insert(AiDecision).values(**decision)
    stmt = stmt.on_conflict_do_update(
        index_elements=["decision_id"],
        set_={k: v for k, v in decision.items() if k != "decision_id"},
    )
    await db.execute(stmt)
    await db.commit()


async def get_decisions(
    db: AsyncSession,
    symbol: Optional[str] = None,
    limit: int = 100,
    since: Optional[datetime] = None,
    decision_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    q = select(AiDecision).order_by(AiDecision.time.desc()).limit(limit)
    if symbol:
        q = q.where(AiDecision.symbol == symbol)
    if since:
        q = q.where(AiDecision.time >= since)
    if decision_type:
        q = q.where(AiDecision.decision == decision_type.upper())
    result = await db.execute(q)
    rows = result.scalars().all()
    return [_decision_to_dict(r) for r in rows]


async def get_recent_trade_outcomes(
    db: AsyncSession,
    symbol: str,
    hours: int = 24,
) -> List[Dict[str, Any]]:
    """Return acted-upon decisions for feedback loop context."""
    cutoff = datetime.now(IST) - timedelta(hours=hours)
    try:
        result = await db.execute(
            select(AiDecision)
            .where(
                AiDecision.symbol == symbol,
                AiDecision.acted_upon == True,
                AiDecision.time >= cutoff,
            )
            .order_by(AiDecision.time.desc())
            .limit(10)
        )
        rows = result.scalars().all()
    except ProgrammingError:
        await db.rollback()
        return []
    return [_decision_to_dict(r) for r in rows]


def _decision_to_dict(row: AiDecision) -> Dict[str, Any]:
    return {
        "decision_id":         row.decision_id,
        "time":                row.time.isoformat(),
        "symbol":              row.symbol,
        "decision":            row.decision,
        "confidence":          float(row.confidence),
        "reasoning":           row.reasoning,
        "stop_loss":           float(row.stop_loss),
        "target":              float(row.target),
        "risk_reward":         float(row.risk_reward),
        "indicators_snapshot": row.indicators_snapshot,
        "acted_upon":          row.acted_upon,
        "trade_id":            row.trade_id,
    }
