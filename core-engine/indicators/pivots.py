"""
Standard pivot point levels.

    Pivot = (H + L + C) / 3
    R1    = 2*P - L
    R2    = P + (H - L)
    R3    = H + 2*(P - L)
    S1    = 2*P - H
    S2    = P - (H - L)
    S3    = L - 2*(H - P)
"""

from models.schemas import PivotLevels


def calculate_pivots(high: float, low: float, close: float) -> PivotLevels:
    pivot = (high + low + close) / 3
    r1 = 2 * pivot - low
    r2 = pivot + (high - low)
    r3 = high + 2 * (pivot - low)
    s1 = 2 * pivot - high
    s2 = pivot - (high - low)
    s3 = low - 2 * (high - pivot)
    return PivotLevels(
        pivot=round(pivot, 2),
        r1=round(r1, 2),
        r2=round(r2, 2),
        r3=round(r3, 2),
        s1=round(s1, 2),
        s2=round(s2, 2),
        s3=round(s3, 2),
    )


def get_nearest_levels(price: float, pivots: PivotLevels, prev_high: float = 0.0, prev_low: float = 0.0) -> dict:
    """Find the nearest support and resistance levels to current price.

    PDH and PDL are included as named levels so the LLM knows when price is
    testing those key boundaries, not just standard pivot math levels.
    """
    levels = {
        "R3": pivots.r3, "R2": pivots.r2, "R1": pivots.r1,
        "Pivot": pivots.pivot,
        "S1": pivots.s1, "S2": pivots.s2, "S3": pivots.s3,
    }
    if prev_high > 0:
        levels["PDH"] = prev_high
    if prev_low > 0:
        levels["PDL"] = prev_low
    above = {k: v for k, v in levels.items() if v > price}
    below = {k: v for k, v in levels.items() if v <= price}

    nearest_resistance = min(above.items(), key=lambda x: x[1]) if above else ("R3", pivots.r3)
    nearest_support = max(below.items(), key=lambda x: x[1]) if below else ("S3", pivots.s3)

    return {
        "nearest_resistance": nearest_resistance[1],
        "nearest_resistance_label": nearest_resistance[0],
        "nearest_support": nearest_support[1],
        "nearest_support_label": nearest_support[0],
    }
