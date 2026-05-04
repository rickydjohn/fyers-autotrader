"""
Repository: market candles + daily indicators.
All writes are upserts (ON CONFLICT DO UPDATE) for idempotency.
"""

import logging
import math
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import DailyIndicator, MarketCandle, OptionsOiSnapshot, SectorBreadthSnapshot

logger = logging.getLogger(__name__)


def _is_missing_relation_error(exc: Exception) -> bool:
    """Detect missing table/view errors from asyncpg-backed ProgrammingError."""
    msg = str(exc).lower()
    return "does not exist" in msg and "relation" in msg


def _sanitize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Replace Decimal NaN/Inf with None so FastAPI can serialize the row."""
    out = {}
    for k, v in row.items():
        if isinstance(v, Decimal):
            try:
                f = float(v)
                out[k] = None if (math.isnan(f) or math.isinf(f)) else v
            except Exception:
                out[k] = None
        else:
            out[k] = v
    return out


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
    return [_sanitize_row(dict(r)) for r in reversed(rows)]


async def upsert_candle(db: AsyncSession, candle: Dict[str, Any]) -> None:
    stmt = insert(MarketCandle).values(**candle)
    # OHLCV always updated; indicator columns only updated when the incoming
    # value is non-NULL so bootstrap (OHLCV-only) never clears live indicators.
    stmt = stmt.on_conflict_do_update(
        index_elements=["time", "symbol"],
        set_={
            "open":   stmt.excluded.open,
            "high":   stmt.excluded.high,
            "low":    stmt.excluded.low,
            "close":  stmt.excluded.close,
            "volume": stmt.excluded.volume,
            "vwap":   func.coalesce(stmt.excluded.vwap,   MarketCandle.vwap),
            "rsi":    func.coalesce(stmt.excluded.rsi,    MarketCandle.rsi),
            "ema_9":  func.coalesce(stmt.excluded.ema_9,  MarketCandle.ema_9),
            "ema_21": func.coalesce(stmt.excluded.ema_21, MarketCandle.ema_21),
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
    until: Optional[datetime] = None,
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

    where_parts = ["symbol = :symbol"]
    params: Dict[str, Any] = {"symbol": symbol, "limit": limit}
    if since:
        where_parts.append(f"{time_col} >= :since")
        params["since"] = since
    if until:
        where_parts.append(f"{time_col} < :until")
        params["until"] = until
    where_clause = " AND ".join(where_parts)

    sql = text(f"""
        SELECT {time_col} AS time, open, high, low, close, volume, vwap_avg AS vwap
        FROM {table}
        WHERE {where_clause}
        ORDER BY {time_col} DESC
        LIMIT :limit
    """) if table != "market_candles" else text(f"""
        SELECT time, open, high, low, close, volume, vwap, rsi, ema_9, ema_21
        FROM {table}
        WHERE {where_clause}
        ORDER BY time DESC
        LIMIT :limit
    """)

    try:
        result = await db.execute(sql, params)
        rows = result.mappings().all()
        return [_sanitize_row(dict(r)) for r in reversed(rows)]
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


async def get_unfilled_gaps(
    db: AsyncSession,
    symbol: str,
    min_gap_pts: float = 100.0,
    lookback_days: int = 90,
) -> List[Dict[str, Any]]:
    """
    Find opening gaps ≥ min_gap_pts (vs prior day's high/low) that remain unfilled.

    Fill condition (OR):
      UP gap   — subsequent day low  ≤ prev_close + 10  OR  low  ≤ prev_high + 10
      DOWN gap — subsequent day high ≥ prev_open  - 10  OR  high ≥ prev_high - 10

    Returns list ordered by gap_date DESC (most recent first).
    """
    sql = text(f"""
        WITH daily_with_prev AS (
            SELECT
                date,
                symbol,
                open,
                high,
                low,
                close,
                LAG(high)  OVER (PARTITION BY symbol ORDER BY date) AS prev_high,
                LAG(low)   OVER (PARTITION BY symbol ORDER BY date) AS prev_low,
                LAG(open)  OVER (PARTITION BY symbol ORDER BY date) AS prev_open,
                LAG(close) OVER (PARTITION BY symbol ORDER BY date) AS prev_close
            FROM daily_ohlcv
            WHERE symbol = :symbol
        ),
        gaps AS (
            SELECT
                date         AS gap_date,
                symbol,
                open         AS gap_open,
                CASE
                    WHEN open > prev_high + :min_gap THEN 'UP'
                    WHEN open < prev_low  - :min_gap THEN 'DOWN'
                END          AS gap_direction,
                prev_high,
                prev_low,
                prev_open,
                prev_close,
                CASE
                    WHEN open > prev_high + :min_gap THEN prev_close::numeric + 10
                    ELSE prev_open::numeric - 10
                END          AS fill_target_1,
                CASE
                    WHEN open > prev_high + :min_gap THEN prev_high::numeric + 10
                    ELSE prev_high::numeric - 10
                END          AS fill_target_2
            FROM daily_with_prev
            WHERE prev_high IS NOT NULL
              AND date >= CURRENT_DATE - INTERVAL '{lookback_days} days'
              AND (
                  open > prev_high + :min_gap
               OR open < prev_low  - :min_gap
              )
        ),
        filled_gaps AS (
            SELECT DISTINCT g.gap_date
            FROM gaps g
            JOIN daily_ohlcv d ON d.symbol = g.symbol AND d.date > g.gap_date
            WHERE
                (g.gap_direction = 'UP'   AND (d.low  <= g.fill_target_1 OR d.low  <= g.fill_target_2))
             OR (g.gap_direction = 'DOWN' AND (d.high >= g.fill_target_1 OR d.high >= g.fill_target_2))
        ),
        td_since AS (
            SELECT g.gap_date, COUNT(d.date) AS td_count
            FROM gaps g
            JOIN daily_ohlcv d ON d.symbol = g.symbol AND d.date > g.gap_date
            GROUP BY g.gap_date
        )
        SELECT
            g.gap_date,
            g.gap_direction,
            g.gap_open,
            g.prev_high,
            g.prev_low,
            g.prev_open,
            g.prev_close,
            g.fill_target_1,
            g.fill_target_2,
            COALESCE(t.td_count, 0) AS trading_days_old,
            CASE
                WHEN g.gap_direction = 'UP'   THEN g.gap_open - g.prev_high
                ELSE g.prev_low - g.gap_open
            END AS gap_size_pts
        FROM gaps g
        LEFT JOIN td_since t ON t.gap_date = g.gap_date
        WHERE g.gap_date NOT IN (SELECT gap_date FROM filled_gaps)
        ORDER BY g.gap_date DESC
    """)
    try:
        result = await db.execute(sql, {"symbol": symbol, "min_gap": min_gap_pts})
        rows = result.mappings().all()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning(f"get_unfilled_gaps failed for {symbol}: {exc}")
        return []


async def get_unfilled_cprs(
    db: AsyncSession,
    symbol: str,
    max_age_trading_days: int = 22,
    lookback_days: int = 60,
) -> List[Dict[str, Any]]:
    """
    Find daily CPR zones (from daily_indicators) that price never touched.
    A CPR zone [min(bc,tc), max(bc,tc)] is considered touched if any subsequent
    day's OHLCV range overlaps with it (low ≤ cpr_high AND high ≥ cpr_low).

    Returns zones ≤ max_age_trading_days old (by trading day count), ordered by
    cpr_date DESC.
    """
    sql = text(f"""
        WITH cprs AS (
            SELECT
                date             AS cpr_date,
                symbol,
                pivot,
                LEAST(bc, tc)    AS cpr_low,
                GREATEST(bc, tc) AS cpr_high,
                cpr_width_pct
            FROM daily_indicators
            WHERE symbol = :symbol
              AND date >= CURRENT_DATE - INTERVAL '{lookback_days} days'
        ),
        breached AS (
            SELECT DISTINCT c.cpr_date
            FROM cprs c
            JOIN daily_ohlcv d ON d.symbol = :symbol AND d.date > c.cpr_date
            WHERE d.low <= c.cpr_high AND d.high >= c.cpr_low
        ),
        td_ages AS (
            SELECT c.cpr_date, COUNT(d.date) AS td_since
            FROM cprs c
            JOIN daily_ohlcv d ON d.symbol = :symbol AND d.date > c.cpr_date
            GROUP BY c.cpr_date
        )
        SELECT
            c.cpr_date,
            c.symbol,
            c.pivot,
            c.cpr_low,
            c.cpr_high,
            c.cpr_width_pct,
            COALESCE(a.td_since, 0) AS trading_days_old
        FROM cprs c
        LEFT JOIN td_ages a ON a.cpr_date = c.cpr_date
        WHERE c.cpr_date NOT IN (SELECT cpr_date FROM breached)
          AND COALESCE(a.td_since, 0) <= :max_age
        ORDER BY c.cpr_date DESC
    """)
    try:
        result = await db.execute(sql, {"symbol": symbol, "max_age": max_age_trading_days})
        rows = result.mappings().all()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.warning(f"get_unfilled_cprs failed for {symbol}: {exc}")
        return []


async def get_prev_day_ohlc(
    db: AsyncSession,
    symbol: str,
    for_date: Optional[date] = None,
) -> Optional[Dict[str, Any]]:
    """Return the most recent trading day's OHLC from daily_ohlcv before for_date."""
    target = for_date or date.today()
    sql = text("""
        SELECT date, open, high, low, close
        FROM daily_ohlcv
        WHERE symbol = :symbol AND date < :target
        ORDER BY date DESC
        LIMIT 1
    """)
    try:
        result = await db.execute(sql, {"symbol": symbol, "target": target})
        row = result.mappings().first()
        if row:
            return {
                "date":  str(row["date"]),
                "open":  float(row["open"]),
                "high":  float(row["high"]),
                "low":   float(row["low"]),
                "close": float(row["close"]),
            }
    except Exception:
        pass
    return None


async def get_monthly_ohlc(
    db: AsyncSession,
    symbol: str,
) -> Optional[Dict[str, Any]]:
    """Return previous calendar month's H/L/C from daily_ohlcv table."""
    today = date.today()
    first_this_month = today.replace(day=1)
    if first_this_month.month == 1:
        first_prev_month = first_this_month.replace(year=first_this_month.year - 1, month=12)
    else:
        first_prev_month = first_this_month.replace(month=first_this_month.month - 1)
    last_prev_month = first_this_month - timedelta(days=1)

    sql = text("""
        SELECT
            MAX(high)  AS high,
            MIN(low)   AS low,
            (SELECT close FROM daily_ohlcv
             WHERE symbol = :symbol AND date >= :start AND date <= :end
             ORDER BY date DESC LIMIT 1) AS close
        FROM daily_ohlcv
        WHERE symbol = :symbol AND date >= :start AND date <= :end
    """)
    try:
        result = await db.execute(
            sql,
            {"symbol": symbol, "start": first_prev_month, "end": last_prev_month},
        )
        row = result.mappings().first()
        if row and row["high"] is not None and row["low"] is not None and row["close"] is not None:
            return {
                "high":  float(row["high"]),
                "low":   float(row["low"]),
                "close": float(row["close"]),
            }
    except Exception:
        pass
    return None


async def get_volume_profile(
    db: AsyncSession,
    symbol: str,
) -> List[Dict[str, Any]]:
    """Return historical average 5m volume per time slot for a symbol."""
    sql = text("""
        SELECT time_slot::TEXT AS time_slot, avg_volume, sample_count
        FROM volume_profile
        WHERE symbol = :symbol
        ORDER BY time_slot
    """)
    result = await db.execute(sql, {"symbol": symbol})
    rows = result.mappings().all()
    return [dict(r) for r in rows]


async def bootstrap_volume_profile(db: AsyncSession) -> None:
    """Populate volume_profile from all existing market_candles data (session hours only).
    Safe to call on every startup — skips if already populated."""
    count_result = await db.execute(text("SELECT COUNT(*) FROM volume_profile"))
    count = count_result.scalar()
    if count and count > 0:
        return

    await db.execute(text("""
        INSERT INTO volume_profile (symbol, time_slot, avg_volume, sample_count)
        WITH bars AS (
            SELECT
                symbol,
                date_trunc('day', time) AS day,
                time_bucket('5 minutes', time - INTERVAL '9 hours 15 minutes')
                    + INTERVAL '9 hours 15 minutes' AS bucket,
                SUM(volume) AS bar_vol
            FROM market_candles
            WHERE volume < 200000000
              AND time::TIME >= '09:15:00'
              AND time::TIME <  '15:31:00'
            GROUP BY symbol, day, bucket
        )
        SELECT
            symbol,
            bucket::TIME AS time_slot,
            AVG(bar_vol)::BIGINT AS avg_volume,
            COUNT(DISTINCT day)::INT AS sample_count
        FROM bars
        GROUP BY symbol, time_slot
        ON CONFLICT (symbol, time_slot) DO UPDATE
            SET avg_volume   = EXCLUDED.avg_volume,
                sample_count = EXCLUDED.sample_count,
                updated_at   = NOW()
    """))
    await db.commit()


async def update_volume_profile_for_date(db: AsyncSession, symbol: str, session_date: str) -> None:
    """Incremental update: recompute volume profile for one session date and merge into averages."""
    await db.execute(text("""
        INSERT INTO volume_profile (symbol, time_slot, avg_volume, sample_count)
        WITH bars AS (
            SELECT
                symbol,
                time_bucket('5 minutes', time - INTERVAL '9 hours 15 minutes')
                    + INTERVAL '9 hours 15 minutes' AS bucket,
                SUM(volume) AS bar_vol
            FROM market_candles
            WHERE symbol = :symbol
              AND time::DATE = :session_date::DATE
              AND volume < 200000000
              AND time::TIME >= '09:15:00'
              AND time::TIME <  '15:31:00'
            GROUP BY symbol, bucket
        )
        SELECT
            symbol,
            bucket::TIME AS time_slot,
            bar_vol::BIGINT AS avg_volume,
            1 AS sample_count
        FROM bars
        ON CONFLICT (symbol, time_slot) DO UPDATE
            SET avg_volume   = ((volume_profile.avg_volume * volume_profile.sample_count) + EXCLUDED.avg_volume)
                               / (volume_profile.sample_count + 1),
                sample_count = volume_profile.sample_count + 1,
                updated_at   = NOW()
    """), {"symbol": symbol, "session_date": session_date})
    await db.commit()


async def insert_options_oi_batch(db: AsyncSession, rows: List[Dict[str, Any]]) -> int:
    """
    Insert a batch of options OI snapshot rows.
    ON CONFLICT DO NOTHING — safe to call multiple times for the same timestamp.
    """
    if not rows:
        return 0
    from datetime import date as date_type
    parsed = []
    for r in rows:
        parsed.append({
            "time":        r["time"],
            "symbol":      r["symbol"],
            "expiry":      date_type.fromisoformat(r["expiry"]) if isinstance(r["expiry"], str) else r["expiry"],
            "strike":      r["strike"],
            "option_type": r["option_type"],
            "ltp":         r.get("ltp"),
            "oi":          r.get("oi"),
            "oi_change":   r.get("oi_change"),
            "volume":      r.get("volume"),
        })
    stmt = insert(OptionsOiSnapshot).values(parsed).on_conflict_do_nothing()
    await db.execute(stmt)
    await db.commit()
    return len(parsed)


async def upsert_sector_breadth(db: AsyncSession, time: datetime, data: Dict[str, Any]) -> None:
    """
    Insert a sector breadth snapshot. ON CONFLICT DO UPDATE so re-runs are idempotent
    (e.g., if the same minute is re-processed the latest data wins).
    """
    stmt = (
        insert(SectorBreadthSnapshot)
        .values(time=time, data=data)
        .on_conflict_do_update(index_elements=["time"], set_={"data": data})
    )
    await db.execute(stmt)
    await db.commit()


async def get_sector_breadth_at(db: AsyncSession, at: datetime) -> Optional[Dict[str, Any]]:
    """Return the most recent sector breadth snapshot at or before `at`."""
    result = await db.execute(
        select(SectorBreadthSnapshot)
        .where(SectorBreadthSnapshot.time <= at)
        .order_by(SectorBreadthSnapshot.time.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    return row.data if row else None
