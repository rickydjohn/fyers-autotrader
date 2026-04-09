"""
Formats a context snapshot dict (received from data-service) into a
compact markdown block for injection into Ollama prompts.
"""

from typing import Any, Dict, List, Optional, Tuple


# ── Price Magnet Zone formatting ──────────────────────────────────────────────

# Fill probability by trading-days age bracket (from backtest analysis)
# Gaps: ~95%+ within first 2 days, decays to ~40% by 90 days
_GAP_FILL_PROB = [
    (0,  2,  0.95),
    (3,  5,  0.88),
    (6,  10, 0.80),
    (11, 20, 0.70),
    (21, 45, 0.55),
    (46, 90, 0.40),
]

# CPR fill probability: 77% within first 22 trading days, then drops to 27.6%
_CPR_FILL_PROB = [
    (0,  3,  0.80),
    (4,  10, 0.72),
    (11, 22, 0.60),
]

# Distance modifier: pct distance from current price to zone centre
_DISTANCE_MOD = [
    (0.0, 0.5,  +0.06),
    (0.5, 1.0,  +0.03),
    (1.0, 2.0,   0.00),
    (2.0, 3.5,  -0.03),
    (3.5, 5.0,  -0.05),
]


def _lookup_prob(table: list, td: int) -> Optional[float]:
    for lo, hi, prob in table:
        if lo <= td <= hi:
            return prob
    return None


def _distance_adj(dist_pct: float) -> float:
    for lo, hi, adj in _DISTANCE_MOD:
        if lo <= dist_pct < hi:
            return adj
    return -0.07  # beyond 5% — very weak


def _pull_label(prob: float) -> str:
    if prob >= 0.80:
        return "HIGH"
    if prob >= 0.65:
        return "MODERATE"
    if prob >= 0.50:
        return "LOW"
    return "NEGLIGIBLE"


def format_magnet_zones(ltp: float, gaps: List[Dict], cprs: List[Dict]) -> str:
    """
    Format unfilled gap and unbreached CPR zones into a compact block for LLM prompts.
    Zones beyond 5% of current price are omitted (too far to influence today's session).
    Returns empty string when no relevant zones exist.
    """
    if not gaps and not cprs:
        return ""

    gap_lines: List[str] = []
    for g in gaps:
        try:
            td = int(g.get("trading_days_old", 0))
            base_prob = _lookup_prob(_GAP_FILL_PROB, td)
            if base_prob is None:
                continue  # too old

            direction = str(g.get("gap_direction", ""))
            fill_t1 = float(g.get("fill_target_1", 0))
            fill_t2 = float(g.get("fill_target_2", 0))
            gap_size = float(g.get("gap_size_pts", 0))

            # Nearest fill target
            zone_centre = (fill_t1 + fill_t2) / 2
            dist_pct = abs(ltp - zone_centre) / ltp * 100
            if dist_pct > 5.0:
                continue  # beyond reach for today

            adj = _distance_adj(dist_pct)
            pull_prob = min(0.95, max(0.30, base_prob + adj))
            if pull_prob < 0.40:
                continue  # negligible signal

            label = _pull_label(pull_prob)
            move_word = "DROP" if direction == "UP" else "RISE"
            trade_bias = "BEARISH magnet — adds confidence to SELL" if direction == "UP" else "BULLISH magnet — adds confidence to BUY"
            gap_date = str(g.get("gap_date", ""))[:10]
            gap_lines.append(
                f"• GAP_{direction} {gap_date} — size={gap_size:.0f}pts — "
                f"fill zone: ₹{fill_t2:.0f}–₹{fill_t1:.0f} | "
                f"age={td}td | pull={label}({pull_prob:.0%}) | "
                f"dist={dist_pct:.1f}% | price needs to {move_word} [{trade_bias}]"
            )
        except Exception:
            continue

    cpr_lines: List[str] = []
    for c in cprs:
        try:
            td = int(c.get("trading_days_old", 0))
            base_prob = _lookup_prob(_CPR_FILL_PROB, td)
            if base_prob is None:
                continue

            cpr_low  = float(c.get("cpr_low",  0))
            cpr_high = float(c.get("cpr_high", 0))
            pivot    = float(c.get("pivot",    0))

            zone_centre = (cpr_low + cpr_high) / 2
            dist_pct = abs(ltp - zone_centre) / ltp * 100
            if dist_pct > 5.0:
                continue

            adj = _distance_adj(dist_pct)
            pull_prob = min(0.95, max(0.30, base_prob + adj))
            if pull_prob < 0.40:
                continue

            label = _pull_label(pull_prob)
            if ltp > zone_centre:
                move_word  = "DROP"
                trade_bias = "BEARISH magnet — adds confidence to SELL"
            else:
                move_word  = "RISE"
                trade_bias = "BULLISH magnet — adds confidence to BUY"

            cpr_date = str(c.get("cpr_date", ""))[:10]
            cpr_lines.append(
                f"• CPR {cpr_date} — zone: ₹{cpr_low:.0f}–₹{cpr_high:.0f} | "
                f"pivot=₹{pivot:.0f} | age={td}td | pull={label}({pull_prob:.0%}) | "
                f"dist={dist_pct:.1f}% | price needs to {move_word} [{trade_bias}]"
            )
        except Exception:
            continue

    if not gap_lines and not cpr_lines:
        return ""

    parts = []
    if gap_lines:
        parts.append("Unfilled Gaps:\n" + "\n".join(gap_lines))
    if cpr_lines:
        parts.append("Unbreached CPR Zones:\n" + "\n".join(cpr_lines))
    return "\n\n".join(parts)


def _detect_confluence(
    daily: Dict[str, Any],
    monthly: Dict[str, Any],
    threshold_pct: float = 0.5,
) -> List[str]:
    """
    Find daily/monthly pivot level pairs that are within threshold_pct of each other.
    Returns a list of human-readable confluence labels.
    """
    if not daily or not monthly:
        return []

    daily_levels = {
        "D-Pivot": daily.get("pivot"),
        "D-BC":    daily.get("bc"),
        "D-TC":    daily.get("tc"),
        "D-R1":    daily.get("r1"),
        "D-R2":    daily.get("r2"),
        "D-S1":    daily.get("s1"),
        "D-S2":    daily.get("s2"),
    }
    monthly_levels = {
        "M-Pivot": monthly.get("pivot"),
        "M-BC":    monthly.get("bc"),
        "M-TC":    monthly.get("tc"),
        "M-R1":    monthly.get("r1"),
        "M-R2":    monthly.get("r2"),
        "M-R3":    monthly.get("r3"),
        "M-S1":    monthly.get("s1"),
        "M-S2":    monthly.get("s2"),
        "M-S3":    monthly.get("s3"),
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
                confluences.append(
                    f"{d_label}≈{m_label} [₹{float(d_val):,.0f}≈₹{float(m_val):,.0f}, {gap_pts:.0f}pts]"
                )
    return confluences


def format_context_for_prompt(ctx: Dict[str, Any]) -> str:
    """Serialize context snapshot into a markdown block for the LLM prompt."""
    prev    = ctx.get("previous_day", {})
    cpr     = ctx.get("today_cpr", {})
    monthly = ctx.get("monthly_cpr", {})
    mtf     = ctx.get("multi_timeframe_trend", {})
    news    = ctx.get("news_sentiment", {})
    vol     = ctx.get("volatility", {})
    sr      = ctx.get("key_levels", {})
    outcomes = ctx.get("recent_trade_outcomes", {})

    prev_str = (
        f"Prev Day — H:₹{prev.get('high', 'N/A')} L:₹{prev.get('low', 'N/A')} C:₹{prev.get('close', 'N/A')}"
        if prev else "Previous day data unavailable."
    )
    cpr_str = (
        f"Today CPR — Pivot:₹{cpr.get('pivot', 'N/A')} BC:₹{cpr.get('bc', 'N/A')} "
        f"TC:₹{cpr.get('tc', 'N/A')} | "
        f"R1:₹{cpr.get('r1', 'N/A')} R2:₹{cpr.get('r2', 'N/A')} | "
        f"S1:₹{cpr.get('s1', 'N/A')} S2:₹{cpr.get('s2', 'N/A')} "
        f"({cpr.get('cpr_type', '')})"
        if cpr else "CPR levels unavailable."
    )

    monthly_str = ""
    if monthly:
        monthly_str = (
            f"Monthly CPR — Pivot:₹{monthly.get('pivot', 'N/A')} BC:₹{monthly.get('bc', 'N/A')} "
            f"TC:₹{monthly.get('tc', 'N/A')} | "
            f"R1:₹{monthly.get('r1', 'N/A')} R2:₹{monthly.get('r2', 'N/A')} R3:₹{monthly.get('r3', 'N/A')} | "
            f"S1:₹{monthly.get('s1', 'N/A')} S2:₹{monthly.get('s2', 'N/A')} S3:₹{monthly.get('s3', 'N/A')}"
        )

    confluence_zones = _detect_confluence(cpr, monthly)
    confluence_str = ""
    if confluence_zones:
        confluence_str = "⚡ Confluence: " + ", ".join(confluence_zones)

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

    lines = [
        "## Historical Context (Multi-Timeframe)",
        prev_str,
        cpr_str,
    ]
    if monthly_str:
        lines.append(monthly_str)
    if confluence_str:
        lines.append(confluence_str)
    lines += [
        f"Trend — 15m:{mtf.get('15m', '?')} | 1h:{mtf.get('1h', '?')} | Daily:{mtf.get('daily', '?')}",
    ]
    if sr_str:
        lines.append(sr_str)
    lines += [
        f"Volatility — 15m ATR:{vol.get('15m_atr_pct', 0):.2f}% | Daily ATR:{vol.get('daily_atr_pct', 0):.2f}%",
        f"News 24h — {news.get('label', 'NEUTRAL')} (score:{news.get('avg_score', 0):.2f}, n={news.get('count', 0)})",
        outcomes_str,
    ]
    return "\n".join(lines)
