"""
Strategy contract.

A strategy is anything with a ``name`` and an ``evaluate(features) -> Signal | None``.
That is the entire extension point: to add a strategy, drop a class in this package
and register it in ``registry.py``. Nothing downstream changes — the screener just
aggregates whatever Signals come back.
"""

from typing import Optional, Protocol

from models import Features, Signal


class Strategy(Protocol):
    name: str

    def evaluate(self, f: Features) -> Optional[Signal]: ...


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))
