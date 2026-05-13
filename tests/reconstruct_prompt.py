"""
Reconstruct the LLM prompt for a specific decision from stored DB data.
Usage: python reconstruct_prompt.py <decision_id>
Default: reconstructs the NIFTY 11:54:45 decision on 2026-05-04
"""

import asyncio
import json
import os
import sys

import asyncpg
import redis.asyncio as aioredis
from datetime import datetime, timezone

sys.path.insert(0, "/app")

from llm.prompts import (
    build_decision_prompt,
    format_options_oi_block,
    format_daily_candles_for_prompt,
    compute_trading_gates,
    format_sector_breadth_block,
)
from indicators.historical_sr import format_sr_for_prompt
from context.formatter import format_context_for_prompt, format_magnet_zones

DB_DSN = os.getenv("DATABASE_URL", "postgresql://trading:trading@timescaledb:5432/trading")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

DECISION_ID = sys.argv[1] if len(sys.argv) > 1 else "c127a16f-25a4-4528-9cdd-bcc1bb7e0103"
SESSION_DATE = "2026-05-04"
DECISION_TIME_UTC = "2026-05-04T06:24:45"   # 11:54:45 IST

# asyncpg requires proper datetime objects — strings with ::cast syntax raise DataError
_DECISION_DT = datetime.fromisoformat(DECISION_TIME_UTC).replace(tzinfo=timezone.utc)
_SESSION_DT  = datetime.fromisoformat(SESSION_DATE).replace(tzinfo=timezone.utc)


async def main():
    pool = await asyncpg.create_pool(DB_DSN)
    r = await aioredis.from_url(REDIS_URL, decode_responses=True)

    async with pool.acquire() as conn:
        # 1. Load indicators snapshot from stored decision
        row = await conn.fetchrow(
            "SELECT indicators_snapshot, symbol FROM llm_decisions WHERE decision_id=$1",
            DECISION_ID,
        )
        if not row:
            print(f"Decision {DECISION_ID} not found in DB", file=sys.stderr)
            return

        ind = json.loads(row["indicators_snapshot"])
        symbol = row["symbol"]
        print(f"Reconstructing prompt for {symbol} @ {DECISION_TIME_UTC} IST", file=sys.stderr)

        # 2. Fetch 1m candles for candle block (~11:35-11:55 IST = 06:05-06:25 UTC)
        candles_raw = await conn.fetch(
            """
            SELECT time AT TIME ZONE 'UTC' AS t, open, high, low, close, volume
            FROM market_candles
            WHERE symbol=$1
              AND resolution='1m'
              AND time >= $2 - INTERVAL '25 minutes'
              AND time <= $2 + INTERVAL '1 minute'
            ORDER BY time
            """,
            symbol, _DECISION_DT,
        )
        recent_candles = [
            {"time": r["t"].isoformat(), "open": float(r["open"]), "high": float(r["high"]),
             "low": float(r["low"]), "close": float(r["close"]), "volume": int(r["volume"] or 0)}
            for r in candles_raw
        ]

        # 3. Fetch daily candles (last 14 sessions)
        daily_raw = await conn.fetch(
            """
            SELECT time AT TIME ZONE 'UTC' AS t, open, high, low, close, volume
            FROM market_candles
            WHERE symbol=$1 AND resolution='1d'
              AND time < $2
            ORDER BY time DESC
            LIMIT 14
            """,
            symbol, _SESSION_DT,
        )
        daily_candles = [
            {"time": r["t"].isoformat(), "open": float(r["open"]), "high": float(r["high"]),
             "low": float(r["low"]), "close": float(r["close"]), "volume": int(r["volume"] or 0)}
            for r in reversed(daily_raw)
        ]

        # 4. Fetch historical S/R levels
        sr_rows = await conn.fetch(
            """
            SELECT level, level_type, strength, first_seen, last_seen
            FROM historical_sr_levels
            WHERE symbol=$1
            ORDER BY level
            """,
            symbol,
        )
        sr_levels = [dict(r) for r in sr_rows]

        # 5. Fetch options OI snapshot nearest to 11:54
        oi_row = await conn.fetchrow(
            """
            SELECT snapshot
            FROM options_oi_snapshots
            WHERE symbol=$1
              AND time <= $2
            ORDER BY time DESC
            LIMIT 1
            """,
            symbol, _DECISION_DT,
        )
        options_oi = json.loads(oi_row["snapshot"]) if oi_row else None

        # 6. Fetch price gaps (magnet zones)
        gap_rows = await conn.fetch(
            """
            SELECT gap_date, gap_direction, gap_size_pts, fill_target_1, fill_target_2,
                   trading_days_old
            FROM price_gaps
            WHERE symbol=$1 AND filled=false
            ORDER BY gap_date DESC
            LIMIT 20
            """,
            symbol,
        )
        gaps = [dict(r) for r in gap_rows]

        # 7. Fetch unbreached CPR zones (magnet zones)
        cpr_rows = await conn.fetch(
            """
            SELECT cpr_date, cpr_low, cpr_high, pivot, trading_days_old
            FROM cpr_zones
            WHERE symbol=$1 AND breached=false
            ORDER BY cpr_date DESC
            LIMIT 20
            """,
            symbol,
        )
        cpr_zones = [dict(r) for r in cpr_rows]

        # 8. Fetch historical context (stored with decision if available)
        hctx_row = await conn.fetchrow(
            "SELECT historical_context FROM llm_decisions WHERE decision_id=$1",
            DECISION_ID,
        )
        historical_context = None
        if hctx_row and hctx_row["historical_context"]:
            historical_context = (
                json.loads(hctx_row["historical_context"])
                if isinstance(hctx_row["historical_context"], str)
                else hctx_row["historical_context"]
            )

    # 9. Fetch sector breadth from Redis
    sector_raw = await r.get(f"sector:breadth:{symbol}")
    sector_breadth = json.loads(sector_raw) if sector_raw else {}

    # 10. Fetch news from Redis
    news_raw = await r.get(f"news:{symbol}")
    news_data = json.loads(news_raw) if news_raw else {}

    # --- Build all the blocks ---
    from indicators.technicals import format_candles_for_prompt, aggregate_1m_to_5m
    from models.schemas import OHLCBar
    from datetime import datetime
    import pytz

    IST = pytz.timezone("Asia/Kolkata")

    # Convert raw 1m candle dicts to OHLCBar objects for format_candles_for_prompt
    ohlc_bars = []
    for c in recent_candles:
        ts_str = c["time"]
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        ohlc_bars.append(OHLCBar(
            timestamp=ts,
            open=c["open"], high=c["high"], low=c["low"],
            close=c["close"], volume=int(c["volume"]),
        ))

    session_dt = datetime.strptime(SESSION_DATE, "%Y-%m-%d").date()
    candle_block = format_candles_for_prompt(ohlc_bars, lookback=12, session_date=session_dt)

    # Daily candle block
    daily_block = format_daily_candles_for_prompt(daily_candles)

    # Historical S/R block
    price = float(ind.get("price", 24226.05))
    sr_block = format_sr_for_prompt(sr_levels, price)

    # Options OI block
    oi_block = format_options_oi_block(options_oi)

    # Magnet zones block
    magnet_block = format_magnet_zones(price, gaps, cpr_zones)

    # Historical context block
    hist_block = ""
    if historical_context:
        hist_block = format_context_for_prompt(historical_context)

    # Sector breadth block
    sector_block = format_sector_breadth_block(sector_breadth) if sector_breadth else ""

    # Trading gates
    gates = compute_trading_gates(
        rsi=float(ind["rsi"]),
        price=price,
        day_low=float(ind["day_low"]),
        day_high=float(ind["day_high"]),
        macd_signal=str(ind["macd_signal"]),
        recent_candles=recent_candles,
    )

    # News summary
    news_items = news_data.get("items", [])
    news_summary_lines = []
    for item in news_items[:5]:
        news_summary_lines.append(f"- {item.get('title', '')} [{item.get('source', '')}]")
    news_summary = "\n".join(news_summary_lines) if news_summary_lines else "No news available."
    news_label = news_data.get("label", "NEUTRAL")
    news_score = float(news_data.get("aggregate_score", 0.074))

    # CPR values from indicators_snapshot context (stored with decision via historical_context)
    if historical_context and historical_context.get("today_cpr"):
        cpr = historical_context["today_cpr"]
        bc = float(cpr.get("bc", 23942.15))
        tc = float(cpr.get("tc", 23979.08))
        pivot = float(cpr.get("pivot", 23960.62))
    else:
        bc, tc, pivot = 23942.15, 23979.08, 23960.62

    prompt = build_decision_prompt(
        symbol=symbol,
        price=price,
        timestamp="2026-05-04 11:54",
        bc=bc,
        tc=tc,
        pivot=pivot,
        cpr_width_pct=float(ind["cpr_width_pct"]),
        cpr_signal=str(ind["cpr_signal"]),
        prev_day_high=float(ind["prev_day_high"]),
        prev_day_low=float(ind["prev_day_low"]),
        day_high=float(ind["day_high"]),
        day_low=float(ind["day_low"]),
        consolidation_pct=float(ind["consolidation_pct"]),
        range_breakout=str(ind["range_breakout"]),
        nearest_resistance=float(ind["nearest_resistance"]),
        resistance_label=str(ind["nearest_resistance_label"]),
        nearest_support=float(ind["nearest_support"]),
        support_label=str(ind["nearest_support_label"]),
        rsi=float(ind["rsi"]),
        ema_9=float(ind["ema_9"]),
        ema_21=float(ind["ema_21"]),
        macd_signal=str(ind["macd_signal"]),
        vwap=float(ind["vwap"]),
        news_summary=news_summary,
        sentiment_label=news_label,
        sentiment_score=news_score,
        historical_context_block=hist_block,
        sr_levels_block=sr_block,
        years_of_data=5,
        day_type=str(ind.get("day_type", "NARROW")),
        magnet_zones_block=magnet_block,
        options_oi_block=oi_block,
        candle_block=candle_block,
        daily_candle_block=daily_block,
        buy_gate=gates["buy_gate"],
        sell_gate=gates["sell_gate"],
        volume_signal=gates["volume_signal"],
        forming_bar_block="",
        sector_breadth_block=sector_block,
    )

    print(prompt)

    await pool.close()
    await r.aclose()


asyncio.run(main())
