"""
APScheduler jobs for market data polling and decision making.
All jobs are IST market-hours aware (09:15 - 15:30, Mon-Fri).

v2: Persists data to TimescaleDB via data-service.
    Fetches multi-timeframe historical context for Ollama prompts.
"""

import asyncio
import json
import logging
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional

import pytz
import redis.asyncio as aioredis
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import settings
from fyers.market_data import get_historical_candles, get_historical_candles_daterange, get_previous_day_ohlc, get_quote
from fyers.greeks import get_option_quote_with_greeks
from models.schemas import OHLCBar
from indicators.cpr import calculate_cpr, get_cpr_signal
from indicators.pivots import calculate_pivots, get_nearest_levels
from indicators.technicals import (
    calculate_consolidation,
    calculate_day_range,
    calculate_ema,
    calculate_macd,
    calculate_rsi,
    calculate_vwap,
)
from indicators.historical_sr import compute_sr_levels, format_sr_for_prompt
from llm.decision import make_decision
from models.schemas import MarketSnapshot, NewsSentiment, TechnicalIndicators
from news.scraper import get_all_news
from news.sentiment import analyze_sentiment
import data_client

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

_news_cache: Optional[NewsSentiment] = None
# Per-symbol historical context cache (refreshed at start of day)
_context_cache: dict = {}
# Per-symbol SR level cache (refreshed on bootstrap + weekly)
_sr_cache: dict = {}   # symbol -> List[dict]


def _is_market_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    open_h, open_m = map(int, settings.market_open.split(":"))
    close_h, close_m = map(int, settings.market_close.split(":"))
    current_minutes = now.hour * 60 + now.minute
    open_minutes = open_h * 60 + open_m
    close_minutes = close_h * 60 + close_m
    return open_minutes <= current_minutes <= close_minutes


async def _refresh_news(redis_client: aioredis.Redis) -> None:
    global _news_cache
    logger.info("Refreshing news feeds...")
    items = await get_all_news()

    # Build seen_titles from either the in-memory cache (fast path, normal operation)
    # or from a recent Redis snapshot of persisted titles (after a restart when
    # _news_cache is None).  This prevents re-inserting the full feed on every restart.
    seen_titles: set = set()
    if _news_cache:
        seen_titles = {i.title.strip().lower() for i in _news_cache.items}
    else:
        # Seed from Redis key written on last successful persist cycle
        cached_raw = await redis_client.get("news:seen_titles")
        if cached_raw:
            import json as _json
            seen_titles = set(_json.loads(cached_raw))

    new_items = [i for i in items if i.title.strip().lower() not in seen_titles]

    _news_cache = analyze_sentiment(items)   # sentiment always uses full set
    await redis_client.setex(
        "news:sentiment",
        3600,
        _news_cache.model_dump_json(),
    )

    # Persist a snapshot of all current titles so the next restart can seed from it
    all_titles = [i.title.strip().lower() for i in items]
    await redis_client.setex("news:seen_titles", 3600 * 12, __import__("json").dumps(all_titles))

    logger.info(
        f"News refreshed: {len(items)} total, {len(new_items)} new, "
        f"sentiment={_news_cache.label}"
    )

    # Persist only the genuinely new headlines to data-service
    if new_items:
        news_payload = [
            {
                "time": item.published_at.isoformat(),
                "title": item.title,
                "summary": item.summary,
                "source": item.source,
                "sentiment_score": item.sentiment_score,
            }
            for item in new_items
        ]
        await data_client.persist_news_batch(news_payload)


async def refresh_context_cache(symbol: str) -> None:
    """Fetch and cache the historical context snapshot for a symbol."""
    global _context_cache
    ctx = await data_client.fetch_context_snapshot(symbol)
    if ctx:
        _context_cache[symbol] = ctx
        logger.info(f"Historical context refreshed for {symbol}")
    else:
        logger.debug(f"No historical context available for {symbol} yet")


async def _process_symbol(symbol: str, redis_client: aioredis.Redis) -> None:
    """Fetch data, compute indicators, get LLM decision for one symbol."""
    logger.info(f"Processing {symbol}...")

    quote = get_quote(symbol)
    if not quote:
        logger.error(f"[SCAN SKIP] {symbol}: get_quote returned None — Fyers auth issue or symbol unsupported")
        return

    candles = get_historical_candles(symbol, interval="5m", limit=100)
    if len(candles) < 30:
        logger.error(f"[SCAN SKIP] {symbol}: only {len(candles)} candles returned (need 30) — insufficient Fyers history")
        return

    prev_ohlc = get_previous_day_ohlc(symbol)
    if not prev_ohlc:
        logger.error(f"[SCAN SKIP] {symbol}: get_previous_day_ohlc returned None — cannot compute CPR/pivots")
        return

    # Compute indicators
    cpr = calculate_cpr(prev_ohlc["high"], prev_ohlc["low"], prev_ohlc["close"])
    pivots = calculate_pivots(prev_ohlc["high"], prev_ohlc["low"], prev_ohlc["close"])
    nearest = get_nearest_levels(quote["ltp"], pivots, prev_ohlc["high"], prev_ohlc["low"])
    rsi = calculate_rsi(candles)
    macd, macd_sig, macd_hist = calculate_macd(candles)
    ema_9 = calculate_ema(candles, 9)
    ema_21 = calculate_ema(candles, 21)
    vwap = calculate_vwap(candles)
    cpr_signal = get_cpr_signal(quote["ltp"], cpr)

    # Intraday range breakout detection
    day_high, day_low = calculate_day_range(candles)
    consolidation_pct, consol_high, consol_low = calculate_consolidation(candles)
    ltp = quote["ltp"]
    # Require price to clear the band by at least 0.05% to avoid noise breakouts
    BREAKOUT_BUFFER = 0.0005
    is_consolidating = consolidation_pct < 0.40
    if is_consolidating and ltp > consol_high * (1 + BREAKOUT_BUFFER):
        range_breakout = "BREAKOUT_HIGH"
    elif is_consolidating and ltp < consol_low * (1 - BREAKOUT_BUFFER):
        range_breakout = "BREAKOUT_LOW"
    else:
        range_breakout = "NONE"

    indicators = TechnicalIndicators(
        cpr=cpr,
        pivots=pivots,
        rsi=rsi,
        vwap=vwap,
        macd=macd,
        macd_signal=macd_sig,
        macd_histogram=macd_hist,
        ema_9=ema_9,
        ema_21=ema_21,
        cpr_signal=cpr_signal,
        prev_day_high=prev_ohlc["high"],
        prev_day_low=prev_ohlc["low"],
        day_high=day_high,
        day_low=day_low,
        consolidation_pct=consolidation_pct,
        range_breakout=range_breakout,
        **nearest,
    )

    snapshot = MarketSnapshot(
        symbol=symbol,
        ltp=quote["ltp"],
        change=quote["change"],
        change_pct=quote["change_pct"],
        volume=quote["volume"],
        timestamp=datetime.now(IST),
        candles=candles[-50:],
        indicators=indicators,
        news=_news_cache,
    )

    # ── Persist to TimescaleDB via data-service ───────────────────────────────
    # 1. Write the latest candle (most recent 1m bar)
    if candles:
        latest = candles[-1]
        await data_client.persist_candle({
            "time":   latest.timestamp.isoformat(),
            "symbol": symbol,
            "open":   latest.open,
            "high":   latest.high,
            "low":    latest.low,
            "close":  latest.close,
            "volume": latest.volume,
            "vwap":   vwap,
            "rsi":    rsi,
            "ema_9":  ema_9,
            "ema_21": ema_21,
        })

    # 2. Write today's daily indicator (CPR / pivots)
    await data_client.persist_daily_indicator({
        "date":          date.today().isoformat(),
        "symbol":        symbol,
        "prev_high":     prev_ohlc["high"],
        "prev_low":      prev_ohlc["low"],
        "prev_close":    prev_ohlc["close"],
        "pivot":         cpr.pivot,
        "bc":            cpr.bc,
        "tc":            cpr.tc,
        "r1":            pivots.r1,
        "r2":            pivots.r2,
        "r3":            pivots.r3,
        "s1":            pivots.s1,
        "s2":            pivots.s2,
        "s3":            pivots.s3,
        "cpr_width_pct": cpr.width_pct,
    })

    # Cache market snapshot in Redis (short-term)
    await redis_client.setex(
        f"market:{symbol}",
        600,
        snapshot.model_dump_json(),
    )

    logger.info(f"[SCAN OK] {symbol}: snapshot written to Redis (ltp={quote['ltp']})")

    # ── Fetch historical context and make LLM decision ────────────────────────
    historical_context = _context_cache.get(symbol)
    sr_levels = _sr_cache.get(symbol, [])
    await make_decision(snapshot, redis_client, historical_context=historical_context, sr_levels=sr_levels)


async def _refresh_open_option_prices(redis_client: aioredis.Redis) -> None:
    """Fetch fresh option LTP for any open option positions and update Redis cache."""
    positions_raw = await redis_client.hgetall("positions:open")
    for _, pos_data in positions_raw.items():
        try:
            pos = json.loads(pos_data)
            option_sym = pos.get("option_symbol")
            if not option_sym:
                continue
            q = get_quote(option_sym)
            if q and q.get("ltp"):
                await redis_client.setex(
                    f"market:{option_sym}",
                    600,
                    json.dumps({"ltp": q["ltp"], "symbol": option_sym}),
                )
        except Exception as e:
            logger.debug(f"Option price refresh failed for position: {e}")


async def _fast_position_watcher(redis_client: aioredis.Redis) -> None:
    """
    High-frequency price + Greeks refresh for open positions.
    Runs every POSITION_WATCHER_INTERVAL_SECONDS (default 10s).
    No-op when the market is closed or no positions are open — zero overhead.

    Writes underlying and option prices with a 30s TTL so stale data is never
    silently used by the simulation-engine exit checker.  Greeks are stored
    separately under greeks:{option_symbol} for the exit-rules engine.
    """
    if not _is_market_open():
        return

    positions_raw = await redis_client.hgetall("positions:open")
    if not positions_raw:
        return

    for symbol, pos_data in positions_raw.items():
        try:
            pos = json.loads(pos_data)

            # 1. Refresh underlying LTP with short TTL.
            # Write to ltp:{symbol} (NOT market:{symbol}) so we don't overwrite the
            # full market snapshot (indicators, candles, etc.) that the full scan
            # writes every 300s.  The simulation-engine reads ltp:{symbol} first.
            q = get_quote(symbol)
            if q and q.get("ltp"):
                await redis_client.setex(
                    f"ltp:{symbol}",
                    30,
                    json.dumps({
                        "ltp":    q["ltp"],
                        "symbol": symbol,
                        "high":   q.get("high", 0),
                        "low":    q.get("low", 0),
                    }),
                )

            # 2. Refresh option LTP + Greeks with short TTL
            opt_sym = pos.get("option_symbol")
            if opt_sym:
                gq = get_option_quote_with_greeks(opt_sym)
                if gq:
                    await redis_client.setex(
                        f"market:{opt_sym}",
                        30,
                        json.dumps({"ltp": gq["ltp"], "symbol": opt_sym}),
                    )
                    await redis_client.setex(
                        f"greeks:{opt_sym}",
                        30,
                        json.dumps(gq),
                    )
        except Exception as e:
            logger.debug(f"Fast position watcher error for {symbol}: {e}")


async def run_market_scan(redis_client: aioredis.Redis) -> None:
    """Main job: scan all symbols if market is open."""
    if not _is_market_open():
        logger.debug("Market closed, skipping scan")
        return
    for symbol in settings.symbols:
        try:
            await _process_symbol(symbol, redis_client)
        except Exception as e:
            logger.exception(f"Error processing {symbol}: {e}")
    await _refresh_open_option_prices(redis_client)


def _chunked_historical_bars(
    symbol: str,
    interval: str,
    lookback_days: int,
    max_chunk_days: int,
) -> List[OHLCBar]:
    """
    Fetch OHLCV across a long lookback using multiple Fyers requests.
    Fyers caps per-request range by resolution (e.g. ~30d for 1m, ~90d for 5m).
    """
    end = datetime.now(IST).date()
    start = end - timedelta(days=lookback_days)
    all_bars: List[OHLCBar] = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=max_chunk_days - 1), end)
        batch = get_historical_candles_daterange(
            symbol,
            interval,
            cur.strftime("%Y-%m-%d"),
            chunk_end.strftime("%Y-%m-%d"),
        )
        all_bars.extend(batch)
        cur = chunk_end + timedelta(days=1)

    by_ts: Dict[datetime, OHLCBar] = {}
    for b in all_bars:
        by_ts[b.timestamp] = b
    return sorted(by_ts.values(), key=lambda x: x.timestamp)


async def _load_sr_cache(symbol: str, redis_client: aioredis.Redis) -> None:
    """Load SR levels from Redis into _sr_cache (fast path used by each scan)."""
    global _sr_cache
    raw = await redis_client.get(f"sr:levels:{symbol}")
    if raw:
        import json as _json
        _sr_cache[symbol] = _json.loads(raw)
    else:
        # Fallback: fetch from data-service and re-cache
        levels = await data_client.fetch_sr_levels(symbol)
        if levels:
            _sr_cache[symbol] = levels
            import json as _json
            await redis_client.setex(f"sr:levels:{symbol}", 86400, _json.dumps(levels, default=str))


async def bootstrap_daily_ohlcv(
    symbol: str,
    redis_client: aioredis.Redis,
    years: int = 5,
) -> Dict[str, Any]:
    """
    Pull `years` of daily OHLCV from Fyers, store in daily_ohlcv (permanent table),
    compute historical S/R levels, persist them, and cache in Redis.

    Fyers limits daily candle requests to 365 days per call — chunked automatically.
    """
    global _sr_cache
    logger.info(f"Bootstrapping {years}yr daily OHLCV for {symbol}...")

    candles = _chunked_historical_bars(
        symbol=symbol,
        interval="1d",
        lookback_days=years * 365,
        max_chunk_days=365,
    )
    if not candles:
        logger.warning(f"No daily candles returned for {symbol}")
        return {"symbol": symbol, "daily_bars": 0, "sr_levels": 0}

    bars = [
        {
            "date":   c.timestamp.strftime("%Y-%m-%d"),
            "symbol": symbol,
            "open":   c.open,
            "high":   c.high,
            "low":    c.low,
            "close":  c.close,
            "volume": c.volume,
        }
        for c in candles
    ]
    await data_client.persist_daily_ohlcv_batch(bars)
    logger.info(f"Persisted {len(bars)} daily bars for {symbol}")

    # Compute S/R levels from the full bar set
    sr_levels = compute_sr_levels(bars, symbol=symbol)
    if sr_levels:
        sr_payload = [
            {
                "level":      z["level"],
                "level_type": z["level_type"],
                "strength":   z["strength"],
                "first_seen": str(z["first_seen"]) if z.get("first_seen") else None,
                "last_seen":  str(z["last_seen"])  if z.get("last_seen")  else None,
            }
            for z in sr_levels
        ]
        await data_client.persist_sr_levels(symbol, sr_payload)

        # Cache in Redis (24h TTL; refreshed weekly by scheduler)
        import json as _json
        await redis_client.setex(
            f"sr:levels:{symbol}",
            86400,
            _json.dumps(sr_payload, default=str),
        )
        _sr_cache[symbol] = sr_payload
        logger.info(f"Computed and cached {len(sr_levels)} S/R levels for {symbol}")

    return {"symbol": symbol, "daily_bars": len(bars), "sr_levels": len(sr_levels)}


async def _refresh_sr_levels(redis_client: aioredis.Redis) -> None:
    """Weekly job: re-pull the latest daily bar and recompute SR levels."""
    for symbol in settings.symbols:
        try:
            await bootstrap_daily_ohlcv(symbol, redis_client)
        except Exception as e:
            logger.warning(f"SR level refresh failed for {symbol}: {e}")


async def bootstrap_historical_data(
    symbol: str,
    redis_client: aioredis.Redis,
) -> Dict[str, Any]:
    """
    Fetch multi-timeframe OHLCV from Fyers and persist to data-service (market_candles).
    Includes 1m (chunked) so the UI timeframe selector has data.
    Intervals / lookbacks follow Fyers per-request limits via max_chunk_days.
    """
    # (interval, lookback_days, max_chunk_days per Fyers request)
    configs = [
        ("1m",  60, 28),   # ~60d of 1m; chunk < 30d limit
        ("5m",  90, 90),
        ("15m", 90, 90),
        ("1h",  180, 180),
        ("1d",  365, 365),
    ]

    per_interval: Dict[str, int] = {}
    total = 0
    for interval, lookback_days, max_chunk in configs:
        candles = _chunked_historical_bars(symbol, interval, lookback_days, max_chunk)
        if not candles:
            logger.warning(f"No {interval} candles returned for {symbol} from Fyers")
            per_interval[interval] = 0
            continue

        batch = [
            {
                "time":   c.timestamp.isoformat(),
                "symbol": symbol,
                "open":   c.open,
                "high":   c.high,
                "low":    c.low,
                "close":  c.close,
                "volume": c.volume,
            }
            for c in candles
        ]
        await data_client.persist_candles_batch(batch)
        n = len(candles)
        per_interval[interval] = n
        total += n
        logger.info(f"Bootstrapped {n} {interval} candles for {symbol}")

    logger.info(f"Historical bootstrap complete for {symbol}: {total} total candles")
    return {"symbol": symbol, "intervals": per_interval, "total_candles": total}


async def _refresh_context_all(redis_client: aioredis.Redis) -> None:
    """Refresh historical context snapshots for all symbols."""
    for symbol in settings.symbols:
        try:
            await refresh_context_cache(symbol)
        except Exception as e:
            logger.warning(f"Context refresh failed for {symbol}: {e}")


def create_scheduler(redis_client: aioredis.Redis) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=IST)

    scheduler.add_job(
        run_market_scan,
        "interval",
        seconds=settings.scan_interval_seconds,
        args=[redis_client],
        id="market_scan",
    )

    scheduler.add_job(
        _fast_position_watcher,
        "interval",
        seconds=settings.position_watcher_interval_seconds,
        args=[redis_client],
        id="fast_position_watcher",
    )

    scheduler.add_job(
        _refresh_news,
        "interval",
        minutes=15,
        args=[redis_client],
        id="news_refresh",
    )

    # Refresh historical context every 5 minutes (cache TTL = 5 min on data-service side)
    scheduler.add_job(
        _refresh_context_all,
        "interval",
        minutes=5,
        args=[redis_client],
        id="context_refresh",
    )

    # Recompute historical S/R levels every Sunday at 08:00 IST (pre-market week prep)
    scheduler.add_job(
        _refresh_sr_levels,
        "cron",
        day_of_week="sun",
        hour=8,
        minute=0,
        args=[redis_client],
        id="sr_levels_weekly",
    )

    return scheduler
