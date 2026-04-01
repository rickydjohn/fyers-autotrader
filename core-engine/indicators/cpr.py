"""
Central Pivot Range (CPR) calculation.

Formulas:
    Pivot  = (High + Low + Close) / 3
    BC     = (High + Low) / 2          # Bottom Central
    TC     = (Pivot - BC) + Pivot      # Top Central
    Width% = (TC - BC) / Pivot * 100

Interpretation:
    - Narrow CPR (< 0.2%) → trending day expected
    - Wide CPR (> 0.5%)   → sideways/rangebound day expected
    - Price above TC      → bullish bias
    - Price inside CPR    → indecision / consolidation
    - Price below BC      → bearish bias
"""

from models.schemas import CPRResult


def calculate_cpr(high: float, low: float, close: float) -> CPRResult:
    pivot = (high + low + close) / 3
    bc = (high + low) / 2
    tc = (pivot - bc) + pivot
    width_pct = abs(tc - bc) / pivot * 100
    return CPRResult(
        pivot=round(pivot, 2),
        bc=round(bc, 2),
        tc=round(tc, 2),
        width_pct=round(width_pct, 4),
        is_narrow=width_pct < 0.25,
    )


def get_cpr_signal(price: float, cpr: CPRResult) -> str:
    if price > cpr.tc:
        return "ABOVE_CPR"
    elif price < cpr.bc:
        return "BELOW_CPR"
    return "INSIDE_CPR"
