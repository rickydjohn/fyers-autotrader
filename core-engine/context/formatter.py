"""
Formats a context snapshot dict (received from data-service) into a
compact markdown block for injection into Ollama prompts.
"""

from typing import Any, Dict


def format_context_for_prompt(ctx: Dict[str, Any]) -> str:
    """Serialize context snapshot into a markdown block for the LLM prompt."""
    prev = ctx.get("previous_day", {})
    cpr  = ctx.get("today_cpr", {})
    mtf  = ctx.get("multi_timeframe_trend", {})
    news = ctx.get("news_sentiment", {})
    vol  = ctx.get("volatility", {})
    sr   = ctx.get("key_levels", {})
    outcomes = ctx.get("recent_trade_outcomes", {})

    prev_str = (
        f"Prev Day — H:₹{prev.get('high', 'N/A')} L:₹{prev.get('low', 'N/A')} C:₹{prev.get('close', 'N/A')}"
        if prev else "Previous day data unavailable."
    )
    cpr_str = (
        f"Today CPR — Pivot:₹{cpr.get('pivot', 'N/A')} BC:₹{cpr.get('bc', 'N/A')} "
        f"TC:₹{cpr.get('tc', 'N/A')} ({cpr.get('cpr_type', '')})"
        if cpr else "CPR levels unavailable."
    )
    sr_str = ""
    if sr:
        sr_str = (
            f"Key Resistance: {sr.get('resistance_zones', [])} | "
            f"Key Support: {sr.get('support_zones', [])}"
        )
    outcomes_str = (
        f"Last {outcomes.get('count', 0)} acted decisions: "
        + "; ".join(
            f"{o['decision']}@{o['confidence']:.0%}" for o in outcomes.get("recent", [])
        )
        if outcomes.get("count", 0) > 0 else "No recent trade outcomes."
    )

    return f"""## Historical Context (Multi-Timeframe)
{prev_str}
{cpr_str}
Trend — 15m:{mtf.get('15m', '?')} | 1h:{mtf.get('1h', '?')} | Daily:{mtf.get('daily', '?')}
{sr_str}
Volatility — 15m ATR:{vol.get('15m_atr_pct', 0):.2f}% | Daily ATR:{vol.get('daily_atr_pct', 0):.2f}%
News 24h — {news.get('label', 'NEUTRAL')} (score:{news.get('avg_score', 0):.2f}, n={news.get('count', 0)})
{outcomes_str}"""
