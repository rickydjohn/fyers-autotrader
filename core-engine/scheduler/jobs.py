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
from models.schemas import OHLCBar
from indicators.cpr import calculate_cpr, get_cpr_signal
from indicators.pivots import calculate_pivots, get_nearest_levels
from indicators.technicals import (
    calculate_ema,
    calculate_macd,
    calculate_rsi,
    calculate_vwap,
)
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
    _news_cache = analyze_sentiment(items)
    await redis_client.setex(
        "news:sentiment",
        3600,
        _news_cache.model_dump_json(),
    )
    logger.info(f"News refreshed: {len(items)} items, sentiment={_news_cache.label}")

    # Persist news to data-service
    news_payload = [
        {
            "time": item.published_at.isoformat(),
            "title": item.title,
            "summary": item.summary,
            "source": item.source,
            "sentiment_score": item.sentiment_score,
        }
        for item in _news_cache.items
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
        logger.warning(f"No quote for {symbol}, skipping")
        return

    candles = get_historical_candles(symbol, interval="5m", limit=100)
    if len(candles) < 30:
        logger.warning(f"Insufficient candles for {symbol}")
        return

    prev_ohlc = get_previous_day_ohlc(symbol)
    if not prev_ohlc:
        logger.warning(f"No prev OHLC for {symbol}")
        return

    # Compute indicators
    cpr = calculate_cpr(prev_ohlc["high"], prev_ohlc["low"], prev_ohlc["close"])
    pivots = calculate_pivots(prev_ohlc["high"], prev_ohlc["low"], prev_ohlc["close"])
    nearest = get_nearest_levels(quote["ltp"], pivots)
    rsi = calculate_rsi(candles)
    macd, macd_sig, macd_hist = calculate_macd(candles)
    ema_9 = calculate_ema(candles, 9)
    ema_21 = calculate_ema(candles, 21)
    vwap = calculate_vwap(candles)
    cpr_signal = get_cpr_signal(quote["ltp"], cpr)

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

    # ── Fetch historical context and make LLM decision ────────────────────────
    historical_context = _context_cache.get(symbol)
    await make_decision(snapshot, redis_client, historical_context=historical_context)


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

    return scheduler
