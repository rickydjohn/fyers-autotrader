"""
Adaptive swing-vs-positional classifier.

Decides which bucket a setup belongs to, which in turn selects the risk/exit
parameters downstream (POSITIONAL = wider ATR stop + trend-ride exit; SWING =
tighter stop + faster target).

Heuristic v1: a clean, not-too-volatile primary uptrend (price above the 200-EMA,
50 above 200) is POSITIONAL — it's worth holding for weeks. Everything else is a
tactical SWING. Strategies may override when they have a strong opinion.
"""

from models import Features, SetupType, TrendRegime

POSITIONAL_MAX_ATR_PCT = 3.5  # too jumpy to hold for weeks above this


def classify(f: Features) -> SetupType:
    primary_uptrend = (
        f.regime == TrendRegime.UPTREND
        and f.ema_200 > 0
        and f.ltp > f.ema_200
        and f.ema_50 >= f.ema_200
    )
    if primary_uptrend and f.atr_pct <= POSITIONAL_MAX_ATR_PCT:
        return SetupType.POSITIONAL
    return SetupType.SWING
