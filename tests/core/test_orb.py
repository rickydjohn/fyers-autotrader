"""
Unit tests for calculate_orb() in core-engine/indicators/technicals.py.

Covers:
- Only 09:15–09:29 candles from today contribute to the ORB
- Pre-market (< 09:15) and post-ORB (>= 09:30) candles are excluded
- Yesterday's candles are excluded
- Returns (0.0, 0.0) when no opening range candles exist
- Returns max-high / min-low across multiple ORB candles
"""

import sys
from datetime import datetime, timedelta

import pytz
import pytest

# test_fast_watcher.py installs a stub for indicators.technicals.
# Evict it so we always import the real implementation.
sys.modules.pop("indicators.technicals", None)
sys.modules.pop("indicators", None)

from indicators.technicals import calculate_orb  # noqa: E402

IST = pytz.timezone("Asia/Kolkata")
_TODAY = datetime.now(IST).date()


def _bar(hour: int, minute: int, high: float, low: float, d=None):
    from models.schemas import OHLCBar
    if d is None:
        d = _TODAY
    ts = IST.localize(datetime(d.year, d.month, d.day, hour, minute, 0))
    return OHLCBar(timestamp=ts, open=high - 10, high=high, low=low, close=high - 5, volume=1000)


class TestCalculateOrbIncluded:
    """Candles inside 09:15–09:29 are included in the ORB."""

    def test_single_09_15_candle(self):
        h, l = calculate_orb([_bar(9, 15, 24500.0, 24400.0)])
        assert h == 24500.0
        assert l == 24400.0

    def test_09_29_is_last_valid_candle(self):
        """09:29 is the last 1m bar that falls strictly inside the ORB window."""
        h, l = calculate_orb([_bar(9, 29, 24600.0, 24450.0)])
        assert h == 24600.0
        assert l == 24450.0

    def test_multiple_candles_take_max_high_min_low(self):
        candles = [
            _bar(9, 15, 24500.0, 24400.0),
            _bar(9, 20, 24550.0, 24380.0),  # highest high, lowest low
            _bar(9, 25, 24520.0, 24420.0),
        ]
        h, l = calculate_orb(candles)
        assert h == 24550.0
        assert l == 24380.0

    def test_five_orb_candles_correct_extremes(self):
        candles = [_bar(9, 15 + i * 3, 24400.0 + i * 10, 24350.0 - i * 5) for i in range(5)]
        h, l = calculate_orb(candles)
        assert h == max(c.high for c in candles)
        assert l == min(c.low for c in candles)


class TestCalculateOrbExcluded:
    """Candles outside 09:15–09:29 are excluded."""

    def test_empty_input_returns_zeros(self):
        assert calculate_orb([]) == (0.0, 0.0)

    def test_09_14_pre_orb_excluded(self):
        assert calculate_orb([_bar(9, 14, 25000.0, 23000.0)]) == (0.0, 0.0)

    def test_09_00_pre_market_excluded(self):
        assert calculate_orb([_bar(9, 0, 25000.0, 23000.0)]) == (0.0, 0.0)

    def test_09_30_excluded_strict_boundary(self):
        """09:30 is the first post-ORB candle — the window is [09:15, 09:30)."""
        assert calculate_orb([_bar(9, 30, 25000.0, 23000.0)]) == (0.0, 0.0)

    def test_post_orb_candle_does_not_widen_range(self):
        """A wide 10:00 candle must not contaminate the ORB high/low."""
        candles = [
            _bar(9, 20, 24500.0, 24400.0),
            _bar(10, 0, 99999.0, 1.0),
        ]
        h, l = calculate_orb(candles)
        assert h == 24500.0
        assert l == 24400.0


class TestCalculateOrbDateFilter:
    """Only today's candles contribute to the ORB."""

    def test_yesterday_candles_excluded(self):
        yesterday = _TODAY - timedelta(days=1)
        assert calculate_orb([_bar(9, 20, 24500.0, 24400.0, d=yesterday)]) == (0.0, 0.0)

    def test_only_todays_candle_used_when_mixed_with_yesterday(self):
        yesterday = _TODAY - timedelta(days=1)
        candles = [
            _bar(9, 20, 99999.0, 1.0, d=yesterday),  # huge range from yesterday — excluded
            _bar(9, 20, 24500.0, 24400.0),             # today — included
        ]
        h, l = calculate_orb(candles)
        assert h == 24500.0
        assert l == 24400.0
