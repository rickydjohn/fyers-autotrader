"""
Central Pivot Range (CPR) calculation.

Formulas:
    Pivot  = (High + Low + Close) / 3
    BC     = (High + Low) / 2          # Bottom Central
    TC     = (Pivot - BC) + Pivot      # Top Central
    Width% = (TC - BC) / Pivot * 100

Day-type classification (ATR-normalised):
    relative_cpr = CPR Width% / daily ATR%
    < 0.20 → NARROW  (trending day)
    0.20–0.35 → MODERATE (weak trend / mixed)
    ≥ 0.35 → WIDE    (rangebound day)

    Fallback (no ATR): NARROW if Width% < 0.25 else WIDE.
"""

from models.schemas import CPRResult


def calculate_cpr(high: float, low: float, close: float, daily_atr_pct: float = 0.0) -> CPRResult:
    pivot = (high + low + close) / 3
    bc = (high + low) / 2
    tc = (pivot - bc) + pivot
    width_pct = abs(tc - bc) / pivot * 100

    if daily_atr_pct > 0:
        relative_cpr = width_pct / daily_atr_pct
        if relative_cpr < 0.20:
            day_type = "NARROW"
        elif relative_cpr < 0.35:
            day_type = "MODERATE"
        else:
            day_type = "WIDE"
    else:
        day_type = "NARROW" if width_pct < 0.25 else "WIDE"

    return CPRResult(
        pivot=round(pivot, 2),
        bc=round(bc, 2),
        tc=round(tc, 2),
        width_pct=round(width_pct, 4),
        is_narrow=day_type == "NARROW",
        day_type=day_type,
    )


def get_cpr_signal(price: float, cpr: CPRResult) -> str:
    cpr_upper = max(cpr.tc, cpr.bc)
    cpr_lower = min(cpr.tc, cpr.bc)
    if price > cpr_upper:
        return "ABOVE_CPR"
    elif price < cpr_lower:
        return "BELOW_CPR"
    return "INSIDE_CPR"
