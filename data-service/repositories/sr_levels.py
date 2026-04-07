"""
Repository: daily_ohlcv and historical_sr_levels tables.
All writes are upserts for idempotency.
"""

from datetime import date
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import DailyOhlcv, HistoricalSRLevel


async def upsert_daily_ohlcv_batch(
    db: AsyncSession,
    bars: List[Dict[str, Any]],
) -> int:
    """
    Batch-upsert daily OHLCV bars.  ON CONFLICT (date, symbol) → update prices.
    Returns the number of rows processed.
    """
    if not bars:
        return 0

    stmt = insert(DailyOhlcv).values(bars)
    stmt = stmt.on_conflict_do_update(
        index_elements=["date", "symbol"],
        set_={
            "open":   stmt.excluded.open,
            "high":   stmt.excluded.high,
            "low":    stmt.excluded.low,
            "close":  stmt.excluded.close,
            "volume": stmt.excluded.volume,
        },
    )
    await db.execute(stmt)
    await db.commit()
    return len(bars)


async def get_daily_ohlcv(
    db: AsyncSession,
    symbol: str,
    since: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """Return all daily bars for symbol, optionally filtered by date, oldest-first."""
    since_clause = "AND date >= :since" if since else ""
    params: Dict[str, Any] = {"symbol": symbol}
    if since:
        params["since"] = since

    result = await db.execute(
        text(f"""
            SELECT date, open, high, low, close, volume
            FROM daily_ohlcv
            WHERE symbol = :symbol {since_clause}
            ORDER BY date ASC
        """),
        params,
    )
    return [dict(r) for r in result.mappings().all()]


async def replace_sr_levels(
    db: AsyncSession,
    symbol: str,
    levels: List[Dict[str, Any]],
) -> int:
    """
    Atomically replace all S/R levels for a symbol.
    Uses INSERT … ON CONFLICT DO UPDATE so the unique index on (symbol, level)
    is respected and old levels at the same price are refreshed rather than
    duplicated.
    """
    if not levels:
        return 0

    # Full replace: delete all existing levels for symbol, then insert fresh set.
    # Simpler than a NOT IN tuple which asyncpg doesn't support as a bind parameter.
    await db.execute(
        text("DELETE FROM historical_sr_levels WHERE symbol = :symbol"),
        {"symbol": symbol},
    )

    rows = [
        {
            "symbol":     symbol,
            "level":      round(float(l["level"]), 2),
            "level_type": l["level_type"],
            "strength":   int(l["strength"]),
            "first_seen": l.get("first_seen"),
            "last_seen":  l.get("last_seen"),
        }
        for l in levels
    ]

    stmt = insert(HistoricalSRLevel).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["symbol", "level"],
        set_={
            "level_type":  stmt.excluded.level_type,
            "strength":    stmt.excluded.strength,
            "first_seen":  stmt.excluded.first_seen,
            "last_seen":   stmt.excluded.last_seen,
            "computed_at": text("NOW()"),
        },
    )
    await db.execute(stmt)
    await db.commit()
    return len(rows)


async def get_sr_levels(
    db: AsyncSession,
    symbol: str,
    near_price: Optional[float] = None,
    pct_band: float = 10.0,
    limit: int = 25,
) -> List[Dict[str, Any]]:
    """
    Fetch S/R levels for symbol.
    If near_price is given, restrict to levels within ±pct_band% of that price.
    """
    band_clause = ""
    params: Dict[str, Any] = {"symbol": symbol, "limit": limit}

    if near_price:
        lo = near_price * (1 - pct_band / 100)
        hi = near_price * (1 + pct_band / 100)
        band_clause = "AND level BETWEEN :lo AND :hi"
        params["lo"] = lo
        params["hi"] = hi

    result = await db.execute(
        text(f"""
            SELECT level, level_type, strength, first_seen, last_seen, computed_at
            FROM historical_sr_levels
            WHERE symbol = :symbol {band_clause}
            ORDER BY strength DESC
            LIMIT :limit
        """),
        params,
    )
    return [dict(r) for r in result.mappings().all()]
