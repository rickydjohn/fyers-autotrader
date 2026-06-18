"""Pluggable signal strategies + the swing/positional classifier."""

from signals.registry import STRATEGIES, generate_signals
from signals.classifier import classify

__all__ = ["STRATEGIES", "generate_signals", "classify"]
