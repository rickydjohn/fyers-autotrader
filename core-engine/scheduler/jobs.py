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
from fyers.market_data import get_historical_candles, get_historical_candles_daterange, get_previous_day_ohlc, get_quote, get_sector_breadth
from fyers.auth import get_fyers_client
from fyers.greeks import get_option_quote_with_greeks
from models.schemas import OHLCBar
from indicators.cpr import calculate_cpr, get_cpr_signal
from indicators.pivots import calculate_pivots, get_nearest_levels
from indicators.technicals import (
    aggregate_1m_to_5m,
    calculate_consolidation,
    calculate_day_range,
    calculate_ema,
    calculate_macd,
    calculate_rsi,
    calculate_vwap,
    format_candles_for_prompt,
)
from indicators.historical_sr import compute_sr_levels, format_sr_for_prompt
from llm.decision import make_decision
from llm.prompts import compute_forming_bar_signal, format_sector_breadth_block
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
# Per-symbol price magnet zones (gaps + CPRs) — bootstrapped at startup, 26h TTL
_magnets_cache: dict = {}  # symbol -> {"gaps": [...], "cprs": [...]}
# Cross-symbol lead-lag cache: stores last published decision per symbol
# Used by Layer 2 gate: NIFTY decision gates BANKNIFTY confidence / direction
_last_decisions: dict = {}  # symbol -> {"decision": str, "confidence": float, "timestamp": float}
# NIFTY is the lead symbol — its decision gates all other symbols
_CROSS_SYMBOL_LEAD = "NSE:NIFTY50-INDEX"
# Maximum age (seconds) for a peer decision to be considered valid for gating
_PEER_SIGNAL_MAX_AGE_S = 900  # 15 minutes
# Per-symbol volume profile cache — refreshed once per day at startup / session open.
# Format: symbol -> {"slots": List[dict], "date": str}
_volume_profile_cache: dict = {}
# Per-symbol 5m candle cache — only re-fetches when a new 5m bar has closed.
# 5m bars close at :00/:05/:10/... so 4 out of 5 scans would otherwise fetch
# identical data. Caching cuts get_historical_candles calls by 80% and
# reduces event-loop blocking proportionally.
_candle_cache: dict = {}  # symbol -> {"candles": List[OHLCBar], "bar_ts": datetime}
# Sector breadth cache — refreshed once per scan, shared across all symbols.
# Holds the pre-formatted prompt block so the format call runs only once.
_sector_breadth_block: str = ""


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


def _get_candles_cached(symbol: str, limit: int = 500) -> List[OHLCBar]:
    """
    Return candles for symbol at settings.candle_interval, fetching from Fyers only
    when a new bar has closed. The bar boundary is derived from the interval so that
    within a single bar window the cached copy is returned and the blocking HTTP call
    is skipped.
    """
    global _candle_cache
    interval = settings.candle_interval
    now = datetime.now(IST)
    base = now.replace(second=0, microsecond=0)

    # Derive bar width in minutes for boundary alignment
    _interval_minutes = {
        "1": 1, "2": 2, "3": 3, "5": 5, "10": 10, "15": 15, "30": 30, "60": 60,
        "1m": 1, "2m": 2, "3m": 3, "5m": 5, "10m": 10, "15m": 15, "30m": 30,
    }
    bar_width = _interval_minutes.get(interval, 1)
    bar_ts = base - timedelta(minutes=base.minute % bar_width)

    cached = _candle_cache.get(symbol)
    if cached and cached["bar_ts"] == bar_ts:
        return cached["candles"]

    candles = get_historical_candles(symbol, interval=interval, limit=limit)
    _candle_cache[symbol] = {"candles": candles, "bar_ts": bar_ts}
    return candles


def _current_bar_position() -> int:
    """Return which minute (0-4) we are in within the current 5m bar.
    Session starts at 09:15 IST; bars align on 5-minute boundaries from there.
    """
    now = datetime.now(IST)
    elapsed = (now.hour * 60 + now.minute) - (9 * 60 + 15)
    if elapsed < 0:
        return 0
    return elapsed % 5


async def _get_volume_profile(symbol: str) -> list:
    """Return volume profile for symbol, fetching from data-service if not cached today."""
    today = date.today().isoformat()
    cached = _volume_profile_cache.get(symbol)
    if cached and cached.get("date") == today and cached.get("slots"):
        return cached["slots"]
    slots = await data_client.fetch_volume_profile(symbol)
    if slots:
        _volume_profile_cache[symbol] = {"slots": slots, "date": today}
    return slots


async def _process_symbol(
    symbol: str,
    redis_client: aioredis.Redis,
    sector_breadth_block: str = "",
) -> None:
    """Fetch data, compute indicators, get LLM decision for one symbol."""
    logger.info(f"Processing {symbol}...")

    quote = get_quote(symbol)
    if not quote:
        logger.error(f"[SCAN SKIP] {symbol}: get_quote returned None — Fyers auth issue or symbol unsupported")
        return

    candles_1m = _get_candles_cached(symbol, limit=500)
    candles = aggregate_1m_to_5m(candles_1m)
    if len(candles) < 30:
        logger.error(f"[SCAN SKIP] {symbol}: only {len(candles)} 5m candles after aggregation (need 30) — insufficient Fyers history")
        return

    prev_ohlc = get_previous_day_ohlc(symbol)
    if not prev_ohlc:
        logger.error(f"[SCAN SKIP] {symbol}: get_previous_day_ohlc returned None — cannot compute CPR/pivots")
        return

    # Compute indicators
    daily_atr_pct = (prev_ohlc["high"] - prev_ohlc["low"]) / prev_ohlc["close"] * 100
    cpr = calculate_cpr(prev_ohlc["high"], prev_ohlc["low"], prev_ohlc["close"], daily_atr_pct=daily_atr_pct)
    pivots = calculate_pivots(prev_ohlc["high"], prev_ohlc["low"], prev_ohlc["close"])
    nearest = get_nearest_levels(quote["ltp"], pivots, prev_ohlc["high"], prev_ohlc["low"])
    # PDH-pivot confluence: PDH within 0.2% of daily Pivot → extra confirmation signal
    pdh_pivot_confluence = abs(prev_ohlc["high"] - cpr.pivot) / cpr.pivot < 0.002
    rsi = calculate_rsi(candles)
    macd, macd_sig, macd_hist = calculate_macd(candles)
    ema_9 = calculate_ema(candles, 9)
    ema_21 = calculate_ema(candles, 21)
    vwap = calculate_vwap(candles)
    candle_block = format_candles_for_prompt(candles, lookback=12)
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
        pdh_pivot_confluence=pdh_pivot_confluence,
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
    # 1. Write all new 1m candles since the last persisted timestamp.
    #    Tracking via Redis prevents gaps when Ollama delays push scans past 60s.
    if candles_1m:
        redis_ts_key = f"last_candle_ts:{symbol}"
        last_ts_raw = await redis_client.get(redis_ts_key)
        if last_ts_raw:
            from datetime import timezone as _tz
            last_ts = datetime.fromisoformat(last_ts_raw).astimezone(_tz.utc)
            new_candles = [c for c in candles_1m if c.timestamp.astimezone(_tz.utc) > last_ts]
        else:
            new_candles = candles_1m[-1:]

        if new_candles:
            # Batch-persist all catchup candles (OHLCV only — no indicators)
            if len(new_candles) > 1:
                await data_client.persist_candles_batch([
                    {
                        "time":   c.timestamp.isoformat(),
                        "symbol": symbol,
                        "open":   c.open,
                        "high":   c.high,
                        "low":    c.low,
                        "close":  c.close,
                        "volume": c.volume,
                    }
                    for c in new_candles[:-1]
                ])

            # Latest candle gets current indicator values
            latest = new_candles[-1]
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

            await redis_client.set(redis_ts_key, new_candles[-1].timestamp.isoformat())

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
    # Skip LLM after session close — candle/indicator data above is still
    # persisted so the chart has a complete view of the trading day.
    _now = datetime.now(IST)
    _close_minutes = settings.session_close_hour * 60 + settings.session_close_minute
    if _now.hour * 60 + _now.minute >= _close_minutes:
        logger.debug(f"[SCAN SKIP LLM] {symbol}: past session close, skipping LLM decision")
        return

    # Timing gate: skip LLM until bar_position >= MIN_BAR_POSITION.
    # Configurable via MIN_BAR_POSITION env var (default 2 = 3rd minute).
    bar_position = _current_bar_position()
    if bar_position < settings.min_bar_position:
        logger.debug(
            f"[SCAN SKIP LLM] {symbol}: bar_position={bar_position} "
            f"(min_bar_position={settings.min_bar_position})"
        )
        return

    # Forming bar signal: analyse the current incomplete 5m bar
    forming_bar_block = ""
    forming_bar_delta = 0.0
    forming_bar_is_bull: Optional[bool] = None
    try:
        volume_profile = await _get_volume_profile(symbol)
        session_start_min = 9 * 60 + 15
        now_ist_min = _now.hour * 60 + _now.minute
        elapsed = now_ist_min - session_start_min
        current_bar_start_min = session_start_min + (elapsed // 5) * 5

        forming_candles = [
            {"time": c.timestamp.isoformat(), "open": c.open, "high": c.high,
             "low": c.low, "close": c.close, "volume": c.volume}
            for c in candles_1m
            if (c.timestamp.astimezone(IST).hour * 60 + c.timestamp.astimezone(IST).minute)
               >= current_bar_start_min
        ]
        fb_signal = compute_forming_bar_signal(forming_candles, bar_position, volume_profile)
        forming_bar_block   = fb_signal.get("forming_bar_block", "")
        forming_bar_delta   = fb_signal.get("confidence_delta", 0.0)
        forming_bar_is_bull = fb_signal.get("forming_bar_is_bull", None)
    except Exception as e:
        logger.debug(f"Could not compute forming bar signal for {symbol}: {e}")
        forming_bar_delta = 0.0

    historical_context = _context_cache.get(symbol)
    sr_levels = _sr_cache.get(symbol, [])
    magnet_zones = _magnets_cache.get(symbol)

    # Read the latest options OI snapshot for this symbol from Redis
    options_oi: Optional[dict] = None
    try:
        oi_raw = await redis_client.get(f"options:chain:{symbol}")
        if oi_raw:
            options_oi = json.loads(oi_raw)
    except Exception as e:
        logger.debug(f"Could not load options OI for {symbol}: {e}")

    # Layer 2 cross-symbol gate: build peer_signal from NIFTY's last decision
    peer_signal: Optional[dict] = None
    if symbol != _CROSS_SYMBOL_LEAD:
        last = _last_decisions.get(_CROSS_SYMBOL_LEAD)
        if last:
            age_s = datetime.now(IST).timestamp() - last["timestamp"]
            if age_s <= _PEER_SIGNAL_MAX_AGE_S:
                peer_signal = last

    decision_result = await make_decision(
        snapshot, redis_client,
        historical_context=historical_context,
        sr_levels=sr_levels,
        magnet_zones=magnet_zones,
        peer_signal=peer_signal,
        options_oi=options_oi,
        candle_block=candle_block,
        raw_candles=candles,
        forming_bar_block=forming_bar_block,
        forming_bar_delta=forming_bar_delta,
        forming_bar_is_bull=forming_bar_is_bull,
        sector_breadth_block=sector_breadth_block,
    )

    # Store final (gated) decision for downstream symbols to use as peer signal
    if decision_result:
        _last_decisions[symbol] = {
            "decision":   decision_result.decision,
            "confidence": decision_result.confidence,
            "timestamp":  decision_result.timestamp.timestamp(),
        }


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
    """Main job: scan all symbols in parallel if market is open.

    asyncio.gather runs all symbols concurrently so each symbol's async LLM
    call overlaps with the others. The blocking Fyers calls (get_quote etc.)
    still execute one-at-a-time (they hold the GIL), but the dominant cost —
    the ~20-45s Ollama inference — runs truly in parallel, keeping the total
    scan time near max(symbol_times) instead of sum(symbol_times).

    Note: BANKNIFTY's cross-symbol peer_signal will use the PREVIOUS scan's
    NIFTY decision (not the current cycle's). That is fine — the gate has a
    15-minute TTL so a 60s-old decision is perfectly valid.
    """
    if not _is_market_open():
        logger.debug("Market closed, skipping scan")
        return

    # Fetch sector sub-index breadth once per scan — shared across all symbols.
    # A single Fyers batch quotes call for 6 sector indices costs one API hit
    # and provides macro conviction context to every LLM decision this cycle.
    global _sector_breadth_block
    try:
        breadth_data = get_sector_breadth()
        _sector_breadth_block = format_sector_breadth_block(breadth_data)
        if breadth_data:
            net = sum(d["change_pct"] * d["weight"] / 100 for d in breadth_data.values())
            sector_parts = "  ".join(
                f"{s} {d['change_pct']:+.2f}%" for s, d in breadth_data.items()
            )
            logger.info(
                f"[SECTOR BREADTH] {len(breadth_data)} sectors, net {net:+.3f}%  |  {sector_parts}"
            )
    except Exception as e:
        logger.warning(f"Sector breadth fetch failed, continuing without it: {e}")
        _sector_breadth_block = ""

    results = await asyncio.gather(
        *[_process_symbol(symbol, redis_client, _sector_breadth_block) for symbol in settings.symbols],
        return_exceptions=True,
    )
    for symbol, result in zip(settings.symbols, results):
        if isinstance(result, Exception):
            logger.exception(f"Error processing {symbol}: {result}")
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


async def _load_magnet_cache(symbol: str, redis_client: aioredis.Redis) -> None:
    """Read magnet zones from Redis into _magnets_cache (fast path per scan)."""
    global _magnets_cache
    import json as _json
    gaps_raw = await redis_client.get(f"magnets:gaps:{symbol}")
    cprs_raw = await redis_client.get(f"magnets:cprs:{symbol}")
    if gaps_raw and cprs_raw:
        _magnets_cache[symbol] = {
            "gaps": _json.loads(gaps_raw),
            "cprs": _json.loads(cprs_raw),
        }
    else:
        logger.debug(f"Magnet zone keys missing from Redis for {symbol} — will bootstrap")


async def bootstrap_magnet_zones(symbol: str, redis_client: aioredis.Redis) -> None:
    """
    Fetch magnet zones from data-service and cache in Redis (26h TTL).
    Called at startup if keys are absent; gracefully skips if data-service has no data.
    """
    global _magnets_cache
    import json as _json

    # Skip if both keys already present (already bootstrapped today)
    gaps_exists = await redis_client.exists(f"magnets:gaps:{symbol}")
    cprs_exists = await redis_client.exists(f"magnets:cprs:{symbol}")
    if gaps_exists and cprs_exists:
        await _load_magnet_cache(symbol, redis_client)
        logger.debug(f"Magnet zones already cached for {symbol}, loaded from Redis")
        return

    zones = await data_client.fetch_magnet_zones(symbol)
    if not zones:
        logger.debug(f"No magnet zone data returned for {symbol} (DB may be empty — skipping)")
        return

    TTL = 26 * 3600
    await redis_client.setex(f"magnets:gaps:{symbol}", TTL, _json.dumps(zones["gaps"], default=str))
    await redis_client.setex(f"magnets:cprs:{symbol}", TTL, _json.dumps(zones["cprs"], default=str))

    _magnets_cache[symbol] = zones
    logger.info(
        f"Magnet zones bootstrapped for {symbol}: "
        f"{len(zones['gaps'])} gaps, {len(zones['cprs'])} CPR zones"
    )


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
        ("1h",  180, 99),
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

        if interval == "1d":
            # Daily candles go to daily_ohlcv, not market_candles.
            # Storing them in market_candles pollutes 1m queries with day-level
            # OHLCV rows (midnight timestamps, 400M+ volume) that corrupt
            # intraday day_high/day_low calculations.
            batch = [
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
            await data_client.persist_daily_ohlcv_batch(batch)
        else:
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


async def _fetch_options_oi_for_symbol(symbol: str, redis_client: aioredis.Redis) -> None:
    """
    Fetch options chain OI snapshot for one symbol.
    Stores full summary → Redis (10-min TTL).
    Stores per-row OI data → TimescaleDB via data-service (90-day retention).
    """
    from collections import defaultdict
    fyers = get_fyers_client()

    # Spot quote
    q = fyers.quotes(data={"symbols": symbol})
    if q.get("s") != "ok":
        logger.warning(f"Options OI [{symbol}]: quote fetch failed")
        return
    v = q["d"][0]["v"]
    spot = v["lp"]

    # Options chain (10 strikes each side)
    resp = fyers.optionchain(data={"symbol": symbol, "strikecount": 10, "timestamp": ""})
    if resp.get("s") != "ok" or "data" not in resp:
        logger.warning(f"Options OI [{symbol}]: chain fetch failed: {resp.get('message')}")
        return

    data       = resp["data"]
    now        = datetime.now(IST)
    vix        = data["indiavixData"]
    expiry_str = data["expiryData"][0]["date"]   # "13-04-2026"

    # Index row carries futures price
    index_row = next((r for r in data["optionsChain"] if r["strike_price"] == -1), {})
    fut = index_row.get("fp", spot)

    # Pair strikes
    strikes: dict = defaultdict(dict)
    for row in data["optionsChain"]:
        if row["strike_price"] == -1:
            continue
        strikes[row["strike_price"]][row["option_type"]] = row

    total_ce_oi = data["callOi"]
    total_pe_oi = data["putOi"]
    pcr = round(total_pe_oi / total_ce_oi, 3) if total_ce_oi else 0

    call_wall = max(
        (sp for sp in strikes if "CE" in strikes[sp]),
        key=lambda sp: strikes[sp]["CE"].get("oi", 0),
    )
    put_wall = max(
        (sp for sp in strikes if "PE" in strikes[sp]),
        key=lambda sp: strikes[sp]["PE"].get("oi", 0),
    )
    max_pain = max(
        strikes,
        key=lambda sp: (
            strikes[sp].get("CE", {}).get("oi", 0)
            + strikes[sp].get("PE", {}).get("oi", 0)
        ),
    )

    # Build chain list
    chain = [
        {
            "strike":  sp,
            "ce_ltp":  strikes[sp].get("CE", {}).get("ltp"),
            "ce_oi":   strikes[sp].get("CE", {}).get("oi"),
            "ce_oich": strikes[sp].get("CE", {}).get("oich"),
            "ce_vol":  strikes[sp].get("CE", {}).get("volume"),
            "pe_ltp":  strikes[sp].get("PE", {}).get("ltp"),
            "pe_oi":   strikes[sp].get("PE", {}).get("oi"),
            "pe_oich": strikes[sp].get("PE", {}).get("oich"),
            "pe_vol":  strikes[sp].get("PE", {}).get("volume"),
        }
        for sp in sorted(strikes)
    ]

    # Store summary in Redis (keyed by symbol)
    await redis_client.setex(
        f"options:chain:{symbol}",
        600,
        json.dumps({
            "timestamp":       now.isoformat(),
            "expiry":          expiry_str,
            "spot":            spot,
            "spot_change_pct": v["chp"],
            "futures":         fut,
            "basis":           round(fut - spot, 2),
            "vix":             vix["ltp"],
            "vix_change_pct":  vix["ltpchp"],
            "pcr":             pcr,
            "total_ce_oi":     total_ce_oi,
            "total_pe_oi":     total_pe_oi,
            "call_wall":       call_wall,
            "call_wall_oi":    strikes[call_wall]["CE"].get("oi"),
            "put_wall":        put_wall,
            "put_wall_oi":     strikes[put_wall]["PE"].get("oi"),
            "max_pain":        max_pain,
            "chain":           chain,
        }),
    )

    # Persist per-row OI to TimescaleDB
    expiry_iso = datetime.strptime(expiry_str, "%d-%m-%Y").date().isoformat()
    rows = [
        {
            "time":        now.isoformat(),
            "symbol":      symbol,
            "expiry":      expiry_iso,
            "strike":      sp,
            "option_type": opt_type,
            "ltp":         row.get("ltp"),
            "oi":          row.get("oi"),
            "oi_change":   row.get("oich"),
            "volume":      row.get("volume"),
        }
        for sp in strikes
        for opt_type, row in strikes[sp].items()
    ]
    await data_client.persist_options_oi_batch(rows)

    logger.info(
        "Options OI [%s] — VIX:%.2f  PCR:%.3f  CallWall:%s  PutWall:%s  MaxPain:%s",
        symbol, vix["ltp"], pcr, call_wall, put_wall, max_pain,
    )


async def _fetch_options_oi(redis_client: aioredis.Redis) -> None:
    """
    Fetch options chain OI snapshot for all configured symbols every 5 minutes.
    Delegates per-symbol work to _fetch_options_oi_for_symbol.
    """
    if not _is_market_open():
        return
    for symbol in settings.symbols:
        try:
            await _fetch_options_oi_for_symbol(symbol, redis_client)
        except Exception as e:
            logger.warning(f"Options OI fetch failed for {symbol}: {e}")


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

    scheduler.add_job(
        _fetch_options_oi,
        "interval",
        minutes=5,
        args=[redis_client],
        id="options_oi_snapshot",
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

    # Update volume profile with today's session data at 15:35 IST (5 min after close)
    scheduler.add_job(
        _update_volume_profile_eod,
        "cron",
        day_of_week="mon-fri",
        hour=15,
        minute=35,
        id="volume_profile_eod",
    )

    return scheduler


async def _update_volume_profile_eod() -> None:
    """Push today's session candles into the volume profile running average."""
    today = date.today().isoformat()
    for symbol in settings.symbols:
        try:
            await data_client.update_volume_profile(symbol, today)
            # Invalidate cache so tomorrow's first scan re-fetches updated profile
            _volume_profile_cache.pop(symbol, None)
            logger.info(f"Volume profile updated for {symbol} ({today})")
        except Exception as e:
            logger.warning(f"Volume profile EOD update failed for {symbol}: {e}")
