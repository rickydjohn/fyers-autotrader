"""
Unit tests for core-engine/indicators/pivots.py — get_nearest_levels.

Covers the fix that adds DayHigh/DayLow/CPR-BC/CPR-TC to the level set so the
sim-engine entry proximity gate can block trades entered at intraday support or
resistance, not just static pivot-math levels.
"""

import sys
import pytest

# test_fast_watcher installs a stub for indicators.pivots in sys.modules.
# Evict it so we always import the real implementation.
sys.modules.pop("indicators.pivots", None)
sys.modules.pop("indicators", None)

from indicators.pivots import calculate_pivots, get_nearest_levels  # noqa: E402


def _make_pivots(high=24500.0, low=24000.0, close=24300.0):
    return calculate_pivots(high, low, close)


class TestGetNearestLevelsBackwardCompat:
    """Existing behaviour unchanged when no intraday levels are passed."""

    def test_nearest_resistance_is_above_price(self):
        pivots = _make_pivots()
        result = get_nearest_levels(24200.0, pivots)
        assert result["nearest_resistance"] > 24200.0

    def test_nearest_support_is_at_or_below_price(self):
        pivots = _make_pivots()
        result = get_nearest_levels(24200.0, pivots)
        assert result["nearest_support"] <= 24200.0

    def test_pdh_included_when_passed(self):
        pivots = _make_pivots()
        # R1 ≈ 24533 with these pivots, so use price=24510 to put PDH=24500
        # as the nearest level below while R1 is still above price.
        result = get_nearest_levels(24510.0, pivots, prev_high=24500.0)
        assert result["nearest_support"] == 24500.0
        assert result["nearest_support_label"] == "PDH"

    def test_pdl_included_when_passed(self):
        pivots = _make_pivots()
        result = get_nearest_levels(23900.0, pivots, prev_low=24000.0)
        assert result["nearest_resistance"] == 24000.0
        assert result["nearest_resistance_label"] == "PDL"


class TestDayHighDayLowIncluded:
    """DayHigh and DayLow are used as named levels when passed."""

    def test_day_low_becomes_nearest_support(self):
        pivots = _make_pivots()
        # Price just above day_low — day_low should be the nearest support
        result = get_nearest_levels(24060.0, pivots, day_low=24050.0)
        assert result["nearest_support"] == 24050.0
        assert result["nearest_support_label"] == "DayLow"

    def test_day_high_becomes_nearest_resistance(self):
        pivots = _make_pivots()
        # Price just below day_high — day_high should be nearest resistance
        result = get_nearest_levels(24390.0, pivots, day_high=24400.0)
        assert result["nearest_resistance"] == 24400.0
        assert result["nearest_resistance_label"] == "DayHigh"

    def test_day_low_beats_distant_pivot_support(self):
        """A day_low closer to price wins over a lower S1/S2."""
        pivots = _make_pivots()
        s1 = pivots.s1  # will be well below 24300
        result_without = get_nearest_levels(24320.0, pivots)
        result_with = get_nearest_levels(24320.0, pivots, day_low=24310.0)
        # Without day_low: nearest_support is s1 or similar (further away)
        assert result_without["nearest_support"] < 24310.0
        # With day_low: nearest_support is the closer day_low
        assert result_with["nearest_support"] == 24310.0
        assert result_with["nearest_support_label"] == "DayLow"

    def test_day_high_beats_distant_pivot_resistance(self):
        """A day_high closer to price wins over a higher R1/R2."""
        pivots = _make_pivots()
        result_without = get_nearest_levels(24380.0, pivots)
        result_with = get_nearest_levels(24380.0, pivots, day_high=24390.0)
        assert result_without["nearest_resistance"] > 24390.0
        assert result_with["nearest_resistance"] == 24390.0
        assert result_with["nearest_resistance_label"] == "DayHigh"

    def test_day_low_zero_is_ignored(self):
        """day_low=0 must not pollute the levels dict."""
        pivots = _make_pivots()
        result = get_nearest_levels(24200.0, pivots, day_low=0.0)
        assert result["nearest_support_label"] != "DayLow"

    def test_day_high_zero_is_ignored(self):
        pivots = _make_pivots()
        result = get_nearest_levels(24200.0, pivots, day_high=0.0)
        assert result["nearest_resistance_label"] != "DayHigh"


class TestCprLevelsIncluded:
    """CPR-BC and CPR-TC are used as named levels when passed."""

    def test_cpr_bc_becomes_nearest_support(self):
        pivots = _make_pivots()
        result = get_nearest_levels(24260.0, pivots, cpr_bc=24250.0)
        assert result["nearest_support"] == 24250.0
        assert result["nearest_support_label"] == "CPR-BC"

    def test_cpr_tc_becomes_nearest_resistance(self):
        pivots = _make_pivots()
        result = get_nearest_levels(24290.0, pivots, cpr_tc=24300.0)
        assert result["nearest_resistance"] == 24300.0
        assert result["nearest_resistance_label"] == "CPR-TC"

    def test_cpr_bc_zero_is_ignored(self):
        pivots = _make_pivots()
        result = get_nearest_levels(24200.0, pivots, cpr_bc=0.0)
        assert result["nearest_support_label"] != "CPR-BC"

    def test_cpr_tc_zero_is_ignored(self):
        pivots = _make_pivots()
        result = get_nearest_levels(24200.0, pivots, cpr_tc=0.0)
        assert result["nearest_resistance_label"] != "CPR-TC"


class TestTodayScenario:
    """
    Reproduces the 24300PE entry that was made at NIFTY 24319 — the day's low.
    The entry proximity gate in sim-engine checks:
        ns <= price <= ns * 1.0025
    With DayLow included this evaluates True and the SELL is blocked.
    """

    def test_sell_at_days_low_would_be_blocked(self):
        pivots = _make_pivots(high=24500.0, low=24000.0, close=24300.0)
        price = 24319.0
        day_low = 24319.0

        result = get_nearest_levels(price, pivots, day_low=day_low)
        ns = result["nearest_support"]
        ns_label = result["nearest_support_label"]

        assert ns_label == "DayLow", f"Expected DayLow, got {ns_label}"
        # Simulate the entry block condition from sim-engine/main.py
        PA_PROXIMITY = 0.0025
        would_block = ns > 0 and ns <= price <= ns * (1 + PA_PROXIMITY)
        assert would_block, (
            f"Entry block should fire: ns={ns} price={price} "
            f"upper={ns * (1 + PA_PROXIMITY):.2f}"
        )

    def test_sell_well_above_days_low_is_not_blocked(self):
        """Price 1% above day_low — not near support, trade allowed."""
        pivots = _make_pivots(high=24500.0, low=24000.0, close=24300.0)
        price = 24400.0
        day_low = 24200.0

        result = get_nearest_levels(price, pivots, day_low=day_low)
        ns = result["nearest_support"]

        PA_PROXIMITY = 0.0025
        would_block = ns > 0 and ns <= price <= ns * (1 + PA_PROXIMITY)
        assert not would_block, "Should not block sell 1% above day_low"

    def test_all_intraday_levels_passed_together(self):
        """When all four intraday levels are passed, nearest wins correctly."""
        pivots = _make_pivots(high=24500.0, low=24000.0, close=24300.0)
        price = 24320.0

        result = get_nearest_levels(
            price, pivots,
            prev_high=24500.0, prev_low=24000.0,
            day_high=24450.0, day_low=24315.0,
            cpr_bc=24280.0, cpr_tc=24350.0,
        )
        # Nearest support below 24320: DayLow=24315 is closer than CPR-BC=24280 or PDL=24000
        assert result["nearest_support"] == 24315.0
        assert result["nearest_support_label"] == "DayLow"
        # Nearest resistance above 24320: CPR-TC=24350 is closer than DayHigh=24450
        assert result["nearest_resistance"] == 24350.0
        assert result["nearest_resistance_label"] == "CPR-TC"
