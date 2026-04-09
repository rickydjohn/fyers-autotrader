"""
Context Builder — assembles a multi-timeframe historical snapshot per symbol.

Called at:
  1. Application startup (bootstrap)
  2. Start of each trading day
  3. On-demand via API (/context-snapshot)

The output is a structured dict that gets serialised into an Ollama base prompt.
"""

import logging
from datetime import datetime, timedelta, date
from typing import Any, Dict, List, Optional

import pytz
from sqlalchemy.ext.asyncio import AsyncSession

from repositories.market_data import get_candles, get_recent_daily_indicators, get_monthly_ohlc
from repositories.decisions import get_recent_trade_outcomes
from repositories.news import get_news_sentiment_summary

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")


def _compute_trend(candles: List[Dict]) -> str:
    """Simple trend from last N candles: BULLISH / BEARISH / SIDEWAYS."""
    if len(candles) < 5:
        return "INSUFFICIENT_DATA"
    closes = [c["close"] for c in candles[-10:]]
    first_half = sum(closes[: len(closes) // 2]) / (len(closes) // 2)
    second_half = sum(closes[len(closes) // 2 :]) / (len(closes) - len(closes) // 2)
    diff_pct = (second_half - first_half) / first_half * 100
    if diff_pct > 0.3:
        return "BULLISH"
    if diff_pct < -0.3:
        return "BEARISH"
    return "SIDEWAYS"


def _compute_volatility(candles: List[Dict]) -> float:
    """Average true range as % of close for recent candles."""
    if len(candles) < 5:
        return 0.0
    ranges = [
        abs(float(c["high"]) - float(c["low"])) / float(c["close"]) * 100
        for c in candles[-20:]
    ]
    return round(sum(ranges) / len(ranges), 3)


def _key_sr_zones(daily_rows: List[Dict]) -> Dict[str, Any]:
    """Extract support/resistance from recent daily highs and lows."""
    if not daily_rows:
        return {}
    highs = sorted({float(r["prev_high"]) for r in daily_rows}, reverse=True)
    lows  = sorted({float(r["prev_low"])  for r in daily_rows})
    return {
        "resistance_zones": highs[:3],
        "support_zones": lows[:3],
    }


async def build_context_snapshot(
    db: AsyncSession,
    symbol: str,
    lookback_days: int = 5,
) -> Dict[str, Any]:
    """
    Returns a structured context dict containing:
    - Yesterday's OHLC and CPR
    - Multi-timeframe trend (15m, 1h, daily)
    - Key support/resistance zones
    - Recent volatility
    - News sentiment (24h)
    - Recent trade outcomes (feedback loop)
    """
    now_ist = datetime.now(IST)
    yesterday = date.today() - timedelta(days=1)

    # ── Daily indicators (CPR / pivots for recent days) ──────────────────────
    daily_rows = await get_recent_daily_indicators(db, symbol, days=lookback_days)
    today_ind: Optional[Dict] = next(
        (r for r in daily_rows if r["date"] == date.today()), None
    )
    yesterday_ind: Optional[Dict] = next(
        (r for r in daily_rows if r["date"] == yesterday), None
    )

    cpr_context: Dict[str, Any] = {}
    prev_day: Dict[str, Any] = {}
    if today_ind:
        cpr_context = {
            "pivot": float(today_ind["pivot"]),
            "bc":    float(today_ind["bc"]),
            "tc":    float(today_ind["tc"]),
            "r1": float(today_ind["r1"]), "r2": float(today_ind["r2"]),
            "s1": float(today_ind["s1"]), "s2": float(today_ind["s2"]),
            "cpr_width_pct": float(today_ind["cpr_width_pct"]),
            "cpr_type": "NARROW (trending day)" if today_ind["cpr_width_pct"] < 0.25 else "WIDE (rangebound day)",
        }
    if yesterday_ind:
        prev_day = {
            "high":  float(yesterday_ind["prev_high"]),
            "low":   float(yesterday_ind["prev_low"]),
            "close": float(yesterday_ind["prev_close"]),
            "date":  str(yesterday),
        }

    # ── Monthly CPR/Pivot levels (from previous calendar month's OHLC) ────────
    monthly_cpr: Dict[str, Any] = {}
    monthly_ohlc = await get_monthly_ohlc(db, symbol)
    if monthly_ohlc:
        mh, ml, mc = monthly_ohlc["high"], monthly_ohlc["low"], monthly_ohlc["close"]
        m_pivot = round((mh + ml + mc) / 3, 2)
        m_bc    = round((mh + ml) / 2, 2)
        m_tc    = round(2 * m_pivot - m_bc, 2)
        m_hl    = mh - ml
        monthly_cpr = {
            "pivot": m_pivot,
            "bc":    m_bc,
            "tc":    m_tc,
            "r1":    round(2 * m_pivot - ml, 2),
            "r2":    round(m_pivot + m_hl, 2),
            "r3":    round(mh + 2 * (m_pivot - ml), 2),
            "s1":    round(2 * m_pivot - mh, 2),
            "s2":    round(m_pivot - m_hl, 2),
            "s3":    round(ml - 2 * (mh - m_pivot), 2),
        }

    # ── Multi-timeframe candles ───────────────────────────────────────────────
    since_7d = now_ist - timedelta(days=7)
    candles_15m = await get_candles(db, symbol, interval="15m", limit=96, since=since_7d)
    candles_1h  = await get_candles(db, symbol, interval="1h",  limit=48, since=since_7d)
    candles_daily = await get_candles(db, symbol, interval="daily", limit=10)

    trend_15m   = _compute_trend(candles_15m)
    trend_1h    = _compute_trend(candles_1h)
    trend_daily = _compute_trend(candles_daily)

    # Most recent intraday values from 15m candles
    recent_15m = candles_15m[-1] if candles_15m else {}
    intraday_high = max((c["high"] for c in candles_15m), default=0)
    intraday_low  = min((c["low"]  for c in candles_15m), default=0)

    # ── Volatility ────────────────────────────────────────────────────────────
    volatility_15m  = _compute_volatility(candles_15m)
    volatility_daily = _compute_volatility(candles_daily)

    # ── Key S/R zones from daily data ─────────────────────────────────────────
    sr_zones = _key_sr_zones(daily_rows)

    # ── News sentiment ────────────────────────────────────────────────────────
    news_summary = await get_news_sentiment_summary(db, hours=24)

    # ── Recent trade outcomes for feedback loop ───────────────────────────────
    recent_outcomes = await get_recent_trade_outcomes(db, symbol, hours=48)
    outcome_summary = _summarise_outcomes(recent_outcomes)

    snapshot = {
        "generated_at": now_ist.isoformat(),
        "symbol": symbol,
        "previous_day": prev_day,
        "today_cpr": cpr_context,
        "monthly_cpr": monthly_cpr,
        "key_levels": sr_zones,
        "multi_timeframe_trend": {
            "15m":   trend_15m,
            "1h":    trend_1h,
            "daily": trend_daily,
        },
        "intraday_range": {
            "high": float(intraday_high),
            "low":  float(intraday_low),
        },
        "volatility": {
            "15m_atr_pct":   volatility_15m,
            "daily_atr_pct": volatility_daily,
        },
        "news_sentiment": news_summary,
        "recent_trade_outcomes": outcome_summary,
    }
    return snapshot


def _summarise_outcomes(outcomes: List[Dict]) -> Dict[str, Any]:
    if not outcomes:
        return {"count": 0, "note": "No recent acted-upon decisions."}
    wins = [o for o in outcomes if o.get("acted_upon")]
    return {
        "count": len(outcomes),
        "recent": [
            {
                "decision": o["decision"],
                "confidence": o["confidence"],
                "reasoning": o["reasoning"][:100],
            }
            for o in outcomes[:3]
        ],
    }


def _detect_confluence(daily: Dict[str, Any], monthly: Dict[str, Any], threshold_pct: float = 0.5) -> List[str]:
    if not daily or not monthly:
        return []
    daily_levels = {
        "D-Pivot": daily.get("pivot"), "D-BC": daily.get("bc"), "D-TC": daily.get("tc"),
        "D-R1": daily.get("r1"), "D-R2": daily.get("r2"),
        "D-S1": daily.get("s1"), "D-S2": daily.get("s2"),
    }
    monthly_levels = {
        "M-Pivot": monthly.get("pivot"), "M-BC": monthly.get("bc"), "M-TC": monthly.get("tc"),
        "M-R1": monthly.get("r1"), "M-R2": monthly.get("r2"), "M-R3": monthly.get("r3"),
        "M-S1": monthly.get("s1"), "M-S2": monthly.get("s2"), "M-S3": monthly.get("s3"),
    }
    confluences = []
    for d_label, d_val in daily_levels.items():
        if d_val is None:
            continue
        for m_label, m_val in monthly_levels.items():
            if m_val is None:
                continue
            gap_pct = abs(float(d_val) - float(m_val)) / float(d_val) * 100
            if gap_pct <= threshold_pct:
                gap_pts = abs(float(d_val) - float(m_val))
                confluences.append(f"{d_label}≈{m_label} [₹{float(d_val):,.0f}≈₹{float(m_val):,.0f}, {gap_pts:.0f}pts]")
    return confluences


def format_context_for_prompt(ctx: Dict[str, Any]) -> str:
    """
    Serialize context snapshot into a compact markdown block
    suitable for prepending to the Ollama decision prompt.
    """
    prev    = ctx.get("previous_day", {})
    cpr     = ctx.get("today_cpr", {})
    monthly = ctx.get("monthly_cpr", {})
    mtf     = ctx.get("multi_timeframe_trend", {})
    news    = ctx.get("news_sentiment", {})
    vol     = ctx.get("volatility", {})
    sr      = ctx.get("key_levels", {})
    outcomes = ctx.get("recent_trade_outcomes", {})

    prev_str = (
        f"Prev Day — H:{prev.get('high', 'N/A')} L:{prev.get('low', 'N/A')} C:{prev.get('close', 'N/A')}"
        if prev else "Previous day data unavailable."
    )
    cpr_str = (
        f"Today CPR — Pivot:{cpr.get('pivot', 'N/A')} BC:{cpr.get('bc', 'N/A')} TC:{cpr.get('tc', 'N/A')} | "
        f"R1:{cpr.get('r1', 'N/A')} R2:{cpr.get('r2', 'N/A')} | "
        f"S1:{cpr.get('s1', 'N/A')} S2:{cpr.get('s2', 'N/A')} ({cpr.get('cpr_type', '')})"
        if cpr else "CPR levels unavailable."
    )
    monthly_str = (
        f"Monthly CPR — Pivot:{monthly.get('pivot', 'N/A')} BC:{monthly.get('bc', 'N/A')} TC:{monthly.get('tc', 'N/A')} | "
        f"R1:{monthly.get('r1', 'N/A')} R2:{monthly.get('r2', 'N/A')} R3:{monthly.get('r3', 'N/A')} | "
        f"S1:{monthly.get('s1', 'N/A')} S2:{monthly.get('s2', 'N/A')} S3:{monthly.get('s3', 'N/A')}"
        if monthly else ""
    )
    confluence_zones = _detect_confluence(cpr, monthly)
    confluence_str = ("⚡ Confluence: " + ", ".join(confluence_zones)) if confluence_zones else ""
    sr_str = (
        f"Resistance zones: {sr.get('resistance_zones', [])} | Support zones: {sr.get('support_zones', [])}"
        if sr else ""
    )
    outcomes_str = (
        f"Last {outcomes.get('count', 0)} acted decisions: "
        + "; ".join(f"{o['decision']}@{o['confidence']:.0%}" for o in outcomes.get("recent", []))
        if outcomes.get("count", 0) > 0 else "No recent trade outcomes."
    )

    lines = ["## Historical Context (Multi-Timeframe)", prev_str, cpr_str]
    if monthly_str:
        lines.append(monthly_str)
    if confluence_str:
        lines.append(confluence_str)
    lines.append(f"Trend — 15m:{mtf.get('15m','?')} | 1h:{mtf.get('1h','?')} | Daily:{mtf.get('daily','?')}")
    if sr_str:
        lines.append(sr_str)
    lines += [
        f"Volatility — 15m ATR:{vol.get('15m_atr_pct', 0):.2f}% | Daily ATR:{vol.get('daily_atr_pct', 0):.2f}%",
        f"News 24h — {news.get('label','NEUTRAL')} (score:{news.get('avg_score', 0):.2f}, n={news.get('count', 0)})",
        outcomes_str,
    ]
    return "\n".join(lines)
