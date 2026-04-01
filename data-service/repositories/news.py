"""
Repository: news items.
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

import pytz

from db.models import NewsItem

IST = pytz.timezone("Asia/Kolkata")


async def insert_news_batch(db: AsyncSession, items: List[Dict[str, Any]]) -> int:
    """Insert news items, skip duplicates by title+time."""
    if not items:
        return 0
    stmt = insert(NewsItem).values(items)
    stmt = stmt.on_conflict_do_nothing()
    result = await db.execute(stmt)
    await db.commit()
    return result.rowcount


async def get_news_sentiment_summary(
    db: AsyncSession,
    hours: int = 24,
) -> Dict[str, Any]:
    cutoff = datetime.now(IST) - timedelta(hours=hours)
    try:
        result = await db.execute(
            select(NewsItem)
            .where(NewsItem.time >= cutoff)
            .order_by(NewsItem.time.desc())
            .limit(50)
        )
        rows = result.scalars().all()
    except ProgrammingError:
        await db.rollback()
        return {"count": 0, "avg_score": 0.0, "label": "NEUTRAL", "headlines": []}
    if not rows:
        return {"count": 0, "avg_score": 0.0, "label": "NEUTRAL", "headlines": []}

    scores = [float(r.sentiment_score) for r in rows]
    avg = sum(scores) / len(scores)
    label = "BULLISH" if avg > 0.1 else "BEARISH" if avg < -0.1 else "NEUTRAL"
    return {
        "count": len(rows),
        "avg_score": round(avg, 3),
        "label": label,
        "headlines": [
            {"title": r.title, "source": r.source, "score": float(r.sentiment_score)}
            for r in rows[:5]
        ],
    }
