"""
Active strategy registry.

The single place that enables/disables strategies. ``generate_signals`` runs them all
against one symbol's features and returns whatever fired. To add a strategy: implement
it (signals/strategies.py) and add an instance here.
"""

from models import Features, Signal
from signals.strategies import Breakout52w, MonthlyCprReclaim, TrendPullback

STRATEGIES = [
    TrendPullback(),
    MonthlyCprReclaim(),
    Breakout52w(),
]


def generate_signals(f: Features) -> list[Signal]:
    signals: list[Signal] = []
    for strat in STRATEGIES:
        sig = strat.evaluate(f)
        if sig is not None and sig.score > 0:
            signals.append(sig)
    return signals
