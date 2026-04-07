"""
Historical Support/Resistance level computation from multi-year daily OHLCV.

Algorithm:
  1. Detect swing highs and lows on the daily chart (±window bar look-around).
  2. Cluster nearby swing prices within `cluster_tolerance_pct` into zones.
  3. Score each zone by touch count + recency + round-number proximity.
  4. Classify each zone: SUPPORT / RESISTANCE / BOTH.
  5. Return top-N zones sorted by score.
"""

import math
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple


# ── Swing detection ────────────────────────────────────────────────────────────

def _find_swings(
    bars: List[Dict[str, Any]],
    window: int,
    use_high: bool,
) -> List[Tuple[float, date]]:
    """
    Find local extrema in the daily bar series.

    A bar at index i is a swing high if its high is strictly the maximum
    within the symmetric window [i-window, i+window].  Lows are analogous.
    """
    key = "high" if use_high else "low"
    fn  = max if use_high else min
    n   = len(bars)
    out: List[Tuple[float, date]] = []

    for i in range(window, n - window):
        candidate = float(bars[i][key])
        neighbours = [float(bars[j][key]) for j in range(i - window, i + window + 1) if j != i]
        if candidate == fn(neighbours + [candidate]) and candidate != fn(neighbours):
            # strictly the best in the window (not just tied)
            bar_date = bars[i]["date"]
            if isinstance(bar_date, str):
                bar_date = date.fromisoformat(bar_date)
            out.append((candidate, bar_date))

    return out


# ── Level clustering ───────────────────────────────────────────────────────────

def _cluster(
    swing_highs: List[Tuple[float, date]],
    swing_lows:  List[Tuple[float, date]],
    tolerance_pct: float,
) -> List[Dict[str, Any]]:
    """
    Merge nearby swing prices into zones.

    Points within `tolerance_pct`% of the running cluster midpoint are merged.
    Returns a list of raw cluster dicts (pre-scoring).
    """
    tagged = (
        [(p, d, "HIGH") for p, d in swing_highs] +
        [(p, d, "LOW")  for p, d in swing_lows]
    )
    if not tagged:
        return []

    tagged.sort(key=lambda x: x[0])

    clusters: List[List[Tuple[float, date, str]]] = []
    current: List[Tuple[float, date, str]] = [tagged[0]]

    for price, d, kind in tagged[1:]:
        mid = sum(p for p, _, _ in current) / len(current)
        if abs(price - mid) / mid * 100 <= tolerance_pct:
            current.append((price, d, kind))
        else:
            clusters.append(current)
            current = [(price, d, kind)]
    clusters.append(current)

    result = []
    for cluster in clusters:
        n_high = sum(1 for _, _, k in cluster if k == "HIGH")
        n_low  = sum(1 for _, _, k in cluster if k == "LOW")
        total  = len(cluster)
        high_ratio = n_high / total

        if high_ratio >= 0.65:
            level_type = "RESISTANCE"
        elif high_ratio <= 0.35:
            level_type = "SUPPORT"
        else:
            level_type = "BOTH"

        prices = [p for p, _, _ in cluster]
        dates  = [d for _, d, _ in cluster]

        result.append({
            "level":      round(sum(prices) / len(prices), 2),
            "level_type": level_type,
            "strength":   total,
            "first_seen": min(dates),
            "last_seen":  max(dates),
        })

    return result


# ── Scoring ────────────────────────────────────────────────────────────────────

def _round_number_proximity(price: float, symbol: str) -> float:
    """
    Return a small bonus (0.0–1.0) if the level is near a psychologically
    significant round number.
      NIFTY50  — multiples of 500
      NIFTYBANK — multiples of 1000
      others   — multiples of 100
    """
    sym = symbol.upper()
    if "NIFTYBANK" in sym or "BANKNIFTY" in sym:
        step = 1000
    elif "NIFTY" in sym:
        step = 500
    else:
        step = 100

    nearest_round = round(price / step) * step
    dist_pct = abs(price - nearest_round) / price * 100
    # Within 0.3% of a round number → bonus up to 1.0
    return max(0.0, 1.0 - dist_pct / 0.3)


def _score(zone: Dict[str, Any], symbol: str, today: Optional[date] = None) -> float:
    """Composite score: strength + recency + round-number proximity."""
    today = today or date.today()

    # Base: raw touch count
    score = float(zone["strength"])

    # Recency: touches in the last 6 months add 50% bonus each
    last_seen = zone["last_seen"]
    if isinstance(last_seen, str):
        last_seen = date.fromisoformat(str(last_seen))
    days_old = (today - last_seen).days
    if days_old <= 180:
        score += 0.5 * (1 - days_old / 180)

    # BOTH type levels are more significant (acted as S and R)
    if zone["level_type"] == "BOTH":
        score += 1.5

    # Round number proximity bonus
    score += _round_number_proximity(zone["level"], symbol)

    return score


# ── Public API ─────────────────────────────────────────────────────────────────

def compute_sr_levels(
    daily_bars: List[Dict[str, Any]],
    symbol: str = "",
    swing_window: int = 5,
    cluster_tolerance_pct: float = 0.5,
    min_strength: int = 2,
    top_n: int = 25,
) -> List[Dict[str, Any]]:
    """
    Compute historical S/R zones from multi-year daily OHLCV bars.

    Args:
        daily_bars:             List of dicts with keys: date, open, high, low, close, volume.
                                Must be sorted oldest-first.
        symbol:                 Symbol string (used for round-number scoring).
        swing_window:           Number of bars each side used to identify a swing point.
                                Default 5 = roughly one trading week.
        cluster_tolerance_pct:  Price levels within this % of each other are merged.
        min_strength:           Zones with fewer touches than this are discarded.
        top_n:                  Maximum zones to return (sorted by score, strongest first).

    Returns:
        List of zone dicts: {level, level_type, strength, first_seen, last_seen, score}
    """
    if len(daily_bars) < swing_window * 2 + 1:
        return []

    highs = _find_swings(daily_bars, swing_window, use_high=True)
    lows  = _find_swings(daily_bars, swing_window, use_high=False)

    zones = _cluster(highs, lows, cluster_tolerance_pct)
    zones = [z for z in zones if z["strength"] >= min_strength]

    today = date.today()
    for z in zones:
        z["score"] = _score(z, symbol, today)

    zones.sort(key=lambda z: z["score"], reverse=True)

    return zones[:top_n]


def nearest_sr_levels(
    zones: List[Dict[str, Any]],
    current_price: float,
    n_above: int = 3,
    n_below: int = 3,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Split zones into resistance (above) and support (below) relative to
    current_price, returning the N closest on each side.
    """
    above = sorted(
        [z for z in zones if z["level"] > current_price],
        key=lambda z: z["level"],
    )[:n_above]

    below = sorted(
        [z for z in zones if z["level"] <= current_price],
        key=lambda z: z["level"],
        reverse=True,
    )[:n_below]

    return {"resistance": above, "support": below}


def format_sr_for_prompt(
    zones: List[Dict[str, Any]],
    current_price: float,
) -> str:
    """
    Build the markdown block injected into the LLM prompt.
    Shows the 3 nearest resistance levels above and 3 support levels below.
    """
    if not zones:
        return "No historical S/R data available yet."

    nearby = nearest_sr_levels(zones, current_price, n_above=3, n_below=3)

    lines = []

    if nearby["resistance"]:
        lines.append("  Resistance above:")
        for z in nearby["resistance"]:
            dist = (z["level"] - current_price) / current_price * 100
            last = str(z.get("last_seen", ""))[:10]
            lines.append(
                f"    ₹{z['level']:,.2f}  {z['level_type']:10s}  "
                f"{z['strength']} tests  last:{last}  [+{dist:.2f}%]"
            )

    if nearby["support"]:
        lines.append("  Support below:")
        for z in nearby["support"]:
            dist = (z["level"] - current_price) / current_price * 100
            last = str(z.get("last_seen", ""))[:10]
            lines.append(
                f"    ₹{z['level']:,.2f}  {z['level_type']:10s}  "
                f"{z['strength']} tests  last:{last}  [{dist:.2f}%]"
            )

    return "\n".join(lines) if lines else "No nearby historical S/R levels."
