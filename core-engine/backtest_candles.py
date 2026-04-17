#!/usr/bin/env python3
"""
Candle-block backtest: replay historical 5m data through the LLM to evaluate
inference quality with the new candle block. No orders placed, no Redis needed.

Run inside the core-engine container:
    docker exec trading-core python /app/backtest_candles.py 2>&1 | tee /tmp/backtest.log
    docker exec trading-core python /app/backtest_candles.py > /tmp/backtest.json 2>/tmp/backtest_progress.log

Results go to stdout as JSON. Progress/decisions go to stderr.
"""
import asyncio
import json
import re
import sys
from datetime import datetime, timedelta
from typing import Optional

import httpx
import pytz

from indicators.cpr import calculate_cpr, get_cpr_signal
from indicators.pivots import calculate_pivots, get_nearest_levels
from indicators.technicals import (
    calculate_consolidation,
    calculate_day_range,
    calculate_ema,
    calculate_macd,
    calculate_rsi,
    calculate_vwap,
    format_candles_for_prompt,
    get_macd_signal_label,
)
from llm.client import get_provider
from llm.prompts import build_decision_prompt
from models.schemas import OHLCBar

IST = pytz.timezone("Asia/Kolkata")
DATA_URL = "http://data-service:8003"

SYMBOLS = ["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX"]
DATES   = ["2026-04-09", "2026-04-10"]

# Scan every N 5m candles (3 = every 15 min — keeps call count ≈ 100 total)
STEP = 3
# Don't scan until at least this many candles have accumulated
MIN_CANDLES = 30


# ── Data fetching ─────────────────────────────────────────────────────────────

async def fetch_5m_candles(symbol: str, date_str: str) -> list[OHLCBar]:
    """Fetch 5m candles for one symbol/date from data-service. Strips daily aggregates."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{DATA_URL}/api/v1/historical-data",
            params={"symbol": symbol, "interval": "5m", "start": date_str, "end": date_str},
        )
        r.raise_for_status()
        data = r.json()

    rows = data.get("candles", data) if isinstance(data, dict) else data
    candles = []
    for row in rows:
        t = datetime.fromisoformat(row["time"])
        if t.tzinfo is None:
            t = IST.localize(t)
        t_ist = t.astimezone(IST)
        # Skip daily aggregate rows (midnight UTC) and rows outside the target date
        if t.hour == 0 and t.minute == 0 and t.second == 0:
            continue
        if t_ist.strftime("%Y-%m-%d") != date_str:
            continue
        candles.append(OHLCBar(
            timestamp=t,
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=int(row["volume"]),
        ))
    return sorted(candles, key=lambda c: c.timestamp)


async def fetch_prev_day_ohlc(symbol: str, backtest_date: str) -> Optional[dict]:
    """
    Fetch prev day OHLC for CPR computation using the daily-indicators endpoint,
    which stores prev_high/prev_low/prev_close for each session date.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{DATA_URL}/api/v1/daily-indicators",
            params={"symbol": symbol, "start": backtest_date, "end": backtest_date},
        )
        r.raise_for_status()
        data = r.json()

    rows = data.get("data", [])
    # Find the row matching the backtest date
    for row in rows:
        if row.get("date") == backtest_date:
            return {
                "high":  float(row["prev_high"]),
                "low":   float(row["prev_low"]),
                "close": float(row["prev_close"]),
                "open":  float(row.get("prev_open", row["prev_close"])),
            }
    return None


# ── Prompt builder ────────────────────────────────────────────────────────────

def build_prompt_from_history(
    symbol: str,
    candles: list[OHLCBar],
    prev_ohlc: dict,
    scan_time: datetime,
) -> str:
    ltp = candles[-1].close

    daily_atr_pct = (prev_ohlc["high"] - prev_ohlc["low"]) / prev_ohlc["close"] * 100
    cpr     = calculate_cpr(prev_ohlc["high"], prev_ohlc["low"], prev_ohlc["close"], daily_atr_pct=daily_atr_pct)
    pivots  = calculate_pivots(prev_ohlc["high"], prev_ohlc["low"], prev_ohlc["close"])
    nearest = get_nearest_levels(ltp, pivots, prev_ohlc["high"], prev_ohlc["low"])

    cpr_signal    = get_cpr_signal(ltp, cpr)
    rsi           = calculate_rsi(candles)
    macd, sig, _  = calculate_macd(candles)
    ema_9         = calculate_ema(candles, 9)
    ema_21        = calculate_ema(candles, 21)
    vwap          = calculate_vwap(candles)
    day_high, day_low = calculate_day_range(candles)
    consol_pct, consol_high, consol_low = calculate_consolidation(candles)
    macd_label    = get_macd_signal_label(macd, sig)
    candle_block  = format_candles_for_prompt(candles, lookback=12)

    BREAKOUT_BUFFER = 0.0005
    if consol_pct < 0.40 and ltp > consol_high * (1 + BREAKOUT_BUFFER):
        range_breakout = "BREAKOUT_HIGH"
    elif consol_pct < 0.40 and ltp < consol_low * (1 - BREAKOUT_BUFFER):
        range_breakout = "BREAKOUT_LOW"
    else:
        range_breakout = "NONE"

    pdh_pivot_confluence = abs(prev_ohlc["high"] - cpr.pivot) / cpr.pivot < 0.002

    return build_decision_prompt(
        symbol=symbol,
        price=ltp,
        timestamp=scan_time.strftime("%Y-%m-%d %H:%M"),
        bc=cpr.bc,
        tc=cpr.tc,
        pivot=cpr.pivot,
        cpr_width_pct=cpr.width_pct,
        cpr_signal=cpr_signal,
        prev_day_high=prev_ohlc["high"],
        prev_day_low=prev_ohlc["low"],
        day_high=day_high,
        day_low=day_low,
        consolidation_pct=consol_pct,
        range_breakout=range_breakout,
        nearest_resistance=nearest["nearest_resistance"],
        resistance_label=nearest["nearest_resistance_label"],
        nearest_support=nearest["nearest_support"],
        support_label=nearest["nearest_support_label"],
        rsi=rsi,
        ema_9=ema_9,
        ema_21=ema_21,
        macd_signal=macd_label,
        vwap=vwap,
        news_summary="No news available (backtest mode).",
        sentiment_label="NEUTRAL",
        sentiment_score=0.0,
        day_type=cpr.day_type,
        pdh_pivot_confluence=pdh_pivot_confluence,
        candle_block=candle_block,
    )


# ── LLM response parser ───────────────────────────────────────────────────────

def parse_response(raw: Optional[str]) -> dict:
    if not raw:
        return {"decision": "NO_RESPONSE", "confidence": 0.0, "reasoning": ""}
    try:
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            parsed = json.loads(m.group())
            return {
                "decision":   parsed.get("decision", "PARSE_ERROR").upper(),
                "confidence": float(parsed.get("confidence", 0.0)),
                "reasoning":  str(parsed.get("reasoning", ""))[:400],
            }
    except Exception:
        pass
    return {"decision": "PARSE_ERROR", "confidence": 0.0, "reasoning": raw[:200]}


# ── Main ──────────────────────────────────────────────────────────────────────

async def run_backtest() -> list[dict]:
    results = []

    for symbol in SYMBOLS:
        for date_str in DATES:
            log(f"\n{'='*65}")
            log(f"  {symbol}  |  {date_str}")
            log(f"{'='*65}")

            candles  = await fetch_5m_candles(symbol, date_str)
            prev_ohlc = await fetch_prev_day_ohlc(symbol, date_str)

            if not candles:
                log(f"  No 5m candles found — skipping")
                continue
            if not prev_ohlc:
                log(f"  No prev day OHLC found — skipping")
                continue

            log(f"  {len(candles)} candles | prev H={prev_ohlc['high']} L={prev_ohlc['low']} C={prev_ohlc['close']}")
            log(f"  {'Time':5}  {'Price':>8}  {'Decision':<6}  {'Conf':>5}  Reasoning (first 90 chars)")
            log(f"  {'-'*80}")

            for i in range(MIN_CANDLES, len(candles), STEP):
                window    = candles[: i + 1]
                last      = window[-1]
                scan_time = last.timestamp.astimezone(IST)

                prompt = build_prompt_from_history(symbol, window, prev_ohlc, scan_time)
                raw    = await get_provider().query(prompt)
                result = parse_response(raw)

                # What did price do in the NEXT 30 min (6 candles) — for hindsight evaluation
                future_candles = candles[i + 1 : i + 7]
                if future_candles:
                    future_close = future_candles[-1].close
                    price_move   = future_close - last.close
                    price_move_pct = price_move / last.close * 100
                    hindsight = f"{price_move:+.0f} ({price_move_pct:+.2f}%) over next 30m"
                else:
                    hindsight = "end of session"

                entry = {
                    "symbol":       symbol,
                    "date":         date_str,
                    "time":         scan_time.strftime("%H:%M"),
                    "price":        last.close,
                    "decision":     result["decision"],
                    "confidence":   result["confidence"],
                    "reasoning":    result["reasoning"],
                    "hindsight_30m": hindsight,
                    "candles_in_window": len(window),
                }
                results.append(entry)

                log(
                    f"  {scan_time.strftime('%H:%M')}  "
                    f"₹{last.close:>8.0f}  "
                    f"{result['decision']:<6}  "
                    f"{result['confidence']:>4.2f}  "
                    f"{result['reasoning'][:90]}"
                )

    return results


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


if __name__ == "__main__":
    results = asyncio.run(run_backtest())
    # Full JSON to stdout so it can be redirected to a file
    print(json.dumps(results, indent=2))
