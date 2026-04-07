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
    """Insert news items, skipping titles already stored in the last 48 hours."""
    if not items:
        return 0

    # Deduplicate within the incoming batch itself first
    seen_in_batch: set = set()
    unique_items = []
    for item in items:
        key = item["title"].strip().lower()
        if key not in seen_in_batch:
            seen_in_batch.add(key)
            unique_items.append(item)

    # Then filter against what's already in the DB
    cutoff = datetime.now(IST) - timedelta(hours=48)
    existing = await db.execute(
        select(NewsItem.title).where(
            NewsItem.time >= cutoff,
            func.lower(func.trim(NewsItem.title)).in_(list(seen_in_batch)),
        )
    )
    seen_in_db = {row[0].strip().lower() for row in existing}
    new_items = [i for i in unique_items if i["title"].strip().lower() not in seen_in_db]
    if not new_items:
        return 0
    await db.execute(insert(NewsItem).values(new_items))
    await db.commit()
    return len(new_items)


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
