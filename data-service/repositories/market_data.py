"""
Repository: market candles + daily indicators.
All writes are upserts (ON CONFLICT DO UPDATE) for idempotency.
"""

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import DailyIndicator, MarketCandle


def _is_missing_relation_error(exc: Exception) -> bool:
    """Detect missing table/view errors from asyncpg-backed ProgrammingError."""
    msg = str(exc).lower()
    return "does not exist" in msg and "relation" in msg


async def _get_bucketed_candles_from_base(
    db: AsyncSession,
    symbol: str,
    interval: str,
    limit: int,
    since: Optional[datetime],
) -> List[Dict[str, Any]]:
    """
    Fallback query for aggregated intervals when continuous aggregate views are absent.
    Computes OHLCV buckets directly from market_candles.
    """
    bucket_map = {
        "5m": "5 minutes",
        "15m": "15 minutes",
        "1h": "1 hour",
        "daily": "1 day",
    }
    bucket = bucket_map[interval]
    since_clause = ""
    params: Dict[str, Any] = {"symbol": symbol, "limit": limit}
    if since:
        since_clause = "AND time >= :since"
        params["since"] = since

    # bucket is an internal constant (not user input), so interpolate directly
    # to avoid asyncpg rejecting a string for an INTERVAL-typed bind parameter.
    sql = text(f"""
        WITH agg AS (
            SELECT
                time_bucket(INTERVAL '{bucket}', time) AS time,
                symbol,
                first(open, time)  AS open,
                max(high)          AS high,
                min(low)           AS low,
                last(close, time)  AS close,
                sum(volume)        AS volume,
                avg(vwap)          AS vwap
            FROM market_candles
            WHERE symbol = :symbol {since_clause}
            GROUP BY 1, 2
        )
        SELECT time, open, high, low, close, volume, vwap
        FROM agg
        ORDER BY time DESC
        LIMIT :limit
    """)
    result = await db.execute(sql, params)
    rows = result.mappings().all()
    return [dict(r) for r in reversed(rows)]


async def upsert_candle(db: AsyncSession, candle: Dict[str, Any]) -> None:
    stmt = insert(MarketCandle).values(**candle)
    stmt = stmt.on_conflict_do_update(
        index_elements=["time", "symbol"],
        set_={
            "open": stmt.excluded.open,
            "high": stmt.excluded.high,
            "low":  stmt.excluded.low,
            "close": stmt.excluded.close,
            "volume": stmt.excluded.volume,
            "vwap":  stmt.excluded.vwap,
            "rsi":   stmt.excluded.rsi,
            "ema_9": stmt.excluded.ema_9,
            "ema_21": stmt.excluded.ema_21,
        },
    )
    await db.execute(stmt)
    await db.commit()


async def upsert_daily_indicator(db: AsyncSession, ind: Dict[str, Any]) -> None:
    stmt = insert(DailyIndicator).values(**ind)
    stmt = stmt.on_conflict_do_update(
        index_elements=["date", "symbol"],
        set_={k: v for k, v in ind.items() if k not in ("date", "symbol")},
    )
    await db.execute(stmt)
    await db.commit()


async def get_candles(
    db: AsyncSession,
    symbol: str,
    interval: str = "1m",
    limit: int = 200,
    since: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch candles from the appropriate view based on interval.
    '1m'    → market_candles
    '5m'    → candles_5m
    '15m'   → candles_15m
    '1h'    → candles_1h
    'daily' → candles_daily
    """
    view_map = {
        "1m":    ("market_candles", "time"),
        "5m":    ("candles_5m",     "bucket"),
        "15m":   ("candles_15m",    "bucket"),
        "1h":    ("candles_1h",     "bucket"),
        "daily": ("candles_daily",  "bucket"),
    }
    table, time_col = view_map.get(interval, ("market_candles", "time"))

    since_clause = ""
    params: Dict[str, Any] = {"symbol": symbol, "limit": limit}
    if since:
        since_clause = f"AND {time_col} >= :since"
        params["since"] = since

    sql = text(f"""
        SELECT {time_col} AS time, open, high, low, close, volume, vwap_avg AS vwap
        FROM {table}
        WHERE symbol = :symbol {since_clause}
        ORDER BY {time_col} DESC
        LIMIT :limit
    """) if table != "market_candles" else text(f"""
        SELECT time, open, high, low, close, volume, vwap, rsi, ema_9, ema_21
        FROM {table}
        WHERE symbol = :symbol {since_clause}
        ORDER BY time DESC
        LIMIT :limit
    """)

    try:
        result = await db.execute(sql, params)
        rows = result.mappings().all()
        return [dict(r) for r in reversed(rows)]
    except ProgrammingError as exc:
        # If continuous aggregate views (candles_5m/15m/1h/daily) are missing
        # in an older DB volume, fallback to computing buckets from base candles.
        if table != "market_candles" and _is_missing_relation_error(exc):
            # The failed query leaves the asyncpg connection in an aborted
            # transaction state — rollback before issuing the fallback query.
            await db.rollback()
            return await _get_bucketed_candles_from_base(
                db=db,
                symbol=symbol,
                interval=interval,
                limit=limit,
                since=since,
            )
        raise


async def get_daily_indicator(
    db: AsyncSession,
    symbol: str,
    for_date: Optional[date] = None,
) -> Optional[Dict[str, Any]]:
    target = for_date or date.today()
    result = await db.execute(
        select(DailyIndicator)
        .where(DailyIndicator.symbol == symbol, DailyIndicator.date == target)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    return {c.key: getattr(row, c.key) for c in DailyIndicator.__table__.columns}


async def get_recent_daily_indicators(
    db: AsyncSession,
    symbol: str,
    days: int = 5,
) -> List[Dict[str, Any]]:
    cutoff = date.today() - timedelta(days=days)
    result = await db.execute(
        select(DailyIndicator)
        .where(DailyIndicator.symbol == symbol, DailyIndicator.date >= cutoff)
        .order_by(DailyIndicator.date.desc())
    )
    rows = result.scalars().all()
    return [{c.key: getattr(r, c.key) for c in DailyIndicator.__table__.columns} for r in rows]
