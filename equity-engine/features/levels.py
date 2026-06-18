"""
Price levels from daily bars.

Two things, both on the DAILY series the user asked to anchor on:

  monthly_cpr() — Central Pivot Range computed from the PRIOR calendar month's
                  high/low/close. Fyers has no monthly resolution, so we aggregate
                  daily bars by month. This is the user's requested level framework
                  (monthly CPR instead of the options system's daily CPR). Formula is
                  identical to core-engine's calculate_cpr (kept inline so the module
                  is self-contained).

  nearest_sr()  — swing-pivot support/resistance: find fractal swing highs/lows over
                  a window, cluster nearby levels, return the nearest level above
                  (resistance) and below (support) the current price.
"""

from collections import defaultdict
from typing import Optional

from models import Bar, MonthlyCPR


def _cpr(high: float, low: float, close: float) -> tuple[float, float, float, float]:
    pivot = (high + low + close) / 3.0
    bc = (high + low) / 2.0
    tc = (pivot - bc) + pivot
    width_pct = abs(tc - bc) / pivot * 100.0 if pivot else 0.0
    return pivot, bc, tc, width_pct


def _position(price: float, tc: float, bc: float) -> str:
    upper, lower = max(tc, bc), min(tc, bc)
    if price > upper:
        return "ABOVE_CPR"
    if price < lower:
        return "BELOW_CPR"
    return "INSIDE_CPR"


def monthly_cpr(bars: list[Bar], price: float) -> Optional[MonthlyCPR]:
    """CPR for the current month, derived from the prior completed month's OHLC."""
    if len(bars) < 25:
        return None

    months: dict[tuple[int, int], list[Bar]] = defaultdict(list)
    for b in bars:
        months[(b.timestamp.year, b.timestamp.month)].append(b)

    keys = sorted(months)
    if len(keys) < 2:
        return None

    prior = months[keys[-2]]   # last fully-completed month
    high = max(b.high for b in prior)
    low = min(b.low for b in prior)
    close = prior[-1].close

    pivot, bc, tc, width_pct = _cpr(high, low, close)
    return MonthlyCPR(
        pivot=round(pivot, 2),
        bc=round(bc, 2),
        tc=round(tc, 2),
        width_pct=round(width_pct, 4),
        position=_position(price, tc, bc),
    )


def _swing_levels(bars: list[Bar], window: int) -> list[float]:
    """Fractal swing highs/lows: a bar that is the extreme of its ±window neighbourhood."""
    levels: list[float] = []
    for i in range(window, len(bars) - window):
        seg = bars[i - window : i + window + 1]
        if bars[i].high >= max(b.high for b in seg):
            levels.append(bars[i].high)
        if bars[i].low <= min(b.low for b in seg):
            levels.append(bars[i].low)
    return levels


def _cluster(levels: list[float], tol_pct: float) -> list[float]:
    """Merge levels within tol_pct of each other into their average."""
    if not levels:
        return []
    levels = sorted(levels)
    clusters: list[list[float]] = [[levels[0]]]
    for lv in levels[1:]:
        if abs(lv - clusters[-1][-1]) / clusters[-1][-1] * 100.0 <= tol_pct:
            clusters[-1].append(lv)
        else:
            clusters.append([lv])
    return [sum(c) / len(c) for c in clusters]


def nearest_sr(
    bars: list[Bar], price: float, window: int = 5, tol_pct: float = 0.75
) -> tuple[float, float]:
    """Return (nearest_support, nearest_resistance) around price. 0.0 when none found."""
    clustered = _cluster(_swing_levels(bars, window), tol_pct)
    supports = [lv for lv in clustered if lv < price]
    resistances = [lv for lv in clustered if lv > price]
    support = max(supports) if supports else 0.0
    resistance = min(resistances) if resistances else 0.0
    return round(support, 2), round(resistance, 2)
