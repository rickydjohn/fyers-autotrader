"""
Unit tests for simulation-engine/execution/exit_rules.py — premium-first architecture.

All tests are pure Python — no Redis, no DB, no Fyers API.
No test data is written anywhere.
"""

from datetime import datetime

import pytz
import pytest

# Evict any stubs other test files may have installed for execution.exit_rules —
# gate test files (test_orb_gate, test_cpr_gate, test_consolidation_gate) _stub
# it at their import time with a minimal subset of attrs, which leaks via
# sys.modules and breaks our full-symbol import below.
import sys as _sys
for _k in ("execution.exit_rules", "execution"):
    _sys.modules.pop(_k, None)

from execution.exit_rules import (
    check_exit,
    DELTA_EROSION_MIN,
    IV_CRUSH_THRESHOLD,
    PREMIUM_SL_PCT,
    FIRST_MILESTONE_PCT,
    RANGING_MILESTONE_PCT,
    MILESTONE_STEP_PCT,
    TRAIL_OFFSET_PCT,
    SESSION_CLOSE_HOUR,
    SESSION_CLOSE_MINUTE,
)
from models.schemas import Position

IST = pytz.timezone("Asia/Kolkata")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pos(
    side="BUY",
    stop_loss=22000.0,
    target=22900.0,
    option_symbol="NSE:NIFTY2640322200CE",
    entry_option_price=100.0,
    peak_option_price=0.0,
    entry_iv=18.0,
    milestone_count=0,
    day_type=None,
) -> Position:
    return Position(
        symbol="NSE:NIFTY50-INDEX",
        side=side,
        quantity=50,
        avg_price=entry_option_price,
        entry_time=datetime(2026, 4, 7, 9, 30, tzinfo=IST),
        stop_loss=stop_loss,
        target=target,
        decision_id="test-decision-id",
        option_symbol=option_symbol,
        entry_option_price=entry_option_price,
        peak_option_price=peak_option_price,
        entry_iv=entry_iv,
        milestone_count=milestone_count,
        day_type=day_type,
    )


def _now(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 4, 7, hour, minute, tzinfo=IST)


def _greeks(delta=0.50, iv=18.0) -> dict:
    return {"delta": delta, "theta": -0.5, "vega": 10.0, "gamma": 0.001, "iv": iv}


def _indicators_bullish() -> dict:
    """RSI > 50, price > VWAP, MACD hist > 0 — all three bullish."""
    return {"rsi": 60, "vwap": 22400.0, "ltp": 22500.0, "macd": 10.0, "macd_signal": 5.0}


def _indicators_bearish() -> dict:
    """RSI < 50, price < VWAP, MACD hist < 0 — all three bearish."""
    return {"rsi": 40, "vwap": 22600.0, "ltp": 22500.0, "macd": 5.0, "macd_signal": 10.0}


def _indicators_neutral() -> dict:
    """Mixed — only 1 of 3 conditions met for BUY or SELL → not confirmed."""
    return {"rsi": 55, "vwap": 22600.0, "ltp": 22500.0, "macd": 5.0, "macd_signal": 10.0}


def _ctx(nearest_resistance=0.0, nearest_resistance_label="R1",
         nearest_support=0.0, nearest_support_label="S1",
         prev_day_high=0.0, prev_day_low=0.0) -> dict:
    return {
        "nearest_resistance": nearest_resistance,
        "nearest_resistance_label": nearest_resistance_label,
        "nearest_support": nearest_support,
        "nearest_support_label": nearest_support_label,
        "prev_day_high": prev_day_high,
        "prev_day_low": prev_day_low,
    }


# ── Rule 1: Session close ─────────────────────────────────────────────────────

class TestSessionClose:
    def test_triggers_at_close_time(self):
        should_exit, reason, _, _ = check_exit(
            _pos(), underlying_ltp=22150.0, option_ltp=110.0, greeks=_greeks(),
            now=_now(SESSION_CLOSE_HOUR, SESSION_CLOSE_MINUTE),
        )
        assert should_exit
        assert reason == "SESSION_CLOSE"

    def test_triggers_after_close_time(self):
        should_exit, reason, _, _ = check_exit(
            _pos(), underlying_ltp=22150.0, option_ltp=110.0, greeks=_greeks(),
            now=_now(SESSION_CLOSE_HOUR, SESSION_CLOSE_MINUTE + 10),
        )
        assert should_exit
        assert reason == "SESSION_CLOSE"

    def test_does_not_trigger_one_minute_before(self):
        # SESSION_CLOSE defaults to 15:15 in settings
        should_exit, _, _, _ = check_exit(
            _pos(), underlying_ltp=22150.0, option_ltp=110.0, greeks=_greeks(),
            now=_now(SESSION_CLOSE_HOUR, SESSION_CLOSE_MINUTE - 1),
        )
        assert not should_exit

    def test_exit_price_is_option_ltp(self):
        _, _, exit_price, _ = check_exit(
            _pos(), underlying_ltp=22150.0, option_ltp=110.0, greeks=_greeks(),
            now=_now(SESSION_CLOSE_HOUR, SESSION_CLOSE_MINUTE),
        )
        assert exit_price == 110.0

    def test_session_close_beats_stop_loss(self):
        """SESSION_CLOSE is highest priority — fires even if stop_loss also triggered."""
        entry = 100.0
        should_exit, reason, _, _ = check_exit(
            _pos(entry_option_price=entry),
            underlying_ltp=22150.0, option_ltp=entry * 0.80,  # below SL floor
            greeks=_greeks(), now=_now(SESSION_CLOSE_HOUR, SESSION_CLOSE_MINUTE),
        )
        assert should_exit
        assert reason == "SESSION_CLOSE"


# ── Rule 2: Premium stop loss (−10%) ─────────────────────────────────────────

class TestPremiumStopLoss:
    def test_triggers_at_sl_floor(self):
        entry = 100.0
        sl_floor = entry * (1 - PREMIUM_SL_PCT)  # ₹90
        should_exit, reason, exit_price, _ = check_exit(
            _pos(entry_option_price=entry),
            underlying_ltp=22150.0, option_ltp=sl_floor, greeks=_greeks(),
            now=_now(10, 0),
        )
        assert should_exit
        assert reason == "STOP_LOSS"
        assert exit_price == sl_floor

    def test_triggers_below_sl_floor(self):
        should_exit, reason, _, _ = check_exit(
            _pos(entry_option_price=100.0),
            underlying_ltp=22150.0, option_ltp=85.0, greeks=_greeks(),
            now=_now(10, 0),
        )
        assert should_exit
        assert reason == "STOP_LOSS"

    def test_exit_price_capped_at_sl_floor_when_price_gaps(self):
        """Price gapped through SL floor — exit must be at floor, not the gapped price."""
        entry = 100.0
        sl_floor = entry * (1 - PREMIUM_SL_PCT)  # ₹90
        gapped_price = 70.0  # gapped well below floor
        _, _, exit_price, _ = check_exit(
            _pos(entry_option_price=entry),
            underlying_ltp=22150.0, option_ltp=gapped_price, greeks=_greeks(),
            now=_now(10, 0),
        )
        assert exit_price == sl_floor  # capped at ₹90, not ₹70

    def test_does_not_trigger_above_floor(self):
        should_exit, _, _, _ = check_exit(
            _pos(entry_option_price=100.0),
            underlying_ltp=22150.0, option_ltp=91.0, greeks=_greeks(),
            now=_now(10, 0),
        )
        assert not should_exit

    def test_skipped_when_no_option_ltp(self):
        should_exit, _, _, _ = check_exit(
            _pos(), underlying_ltp=22150.0, option_ltp=None, greeks=_greeks(),
            now=_now(10, 0),
        )
        assert not should_exit

    def test_skipped_when_entry_price_zero(self):
        pos = _pos(entry_option_price=0.0)
        should_exit, _, _, _ = check_exit(
            pos, underlying_ltp=22150.0, option_ltp=5.0, greeks=_greeks(),
            now=_now(10, 0),
        )
        assert not should_exit


# ── Rule 3 (formerly PA exit): Trail engagement at S/R proximity ──────────────
# When underlying is within 0.25% of nearest S/R in the trade direction AND
# the position is in profit AND milestone == 0, engage the trail (set
# milestone to 1) so the existing TRAIL_FLOOR rule handles the actual exit.
# Backtest of 136 historical PA fires showed +₹20,592 net improvement over
# outright exit (commit referencing backtest_pa_exit_vs_trail.py).

class TestPriceActionTrailEngagement:
    """PA_SUPPORT / PA_RESISTANCE now ENGAGE the trail instead of exiting."""

    # ── CE side: PA_RESISTANCE engages trail ──
    def test_ce_engages_trail_when_near_resistance_in_profit(self):
        """BUY/CE with underlying right at resistance and option up → trail engaged, NOT exited."""
        pos = _pos(side="BUY", entry_option_price=100.0)
        should_exit, reason, _, new_milestone = check_exit(
            pos, underlying_ltp=22500.0, option_ltp=120.0, greeks=_greeks(),
            now=_now(11, 0),
            market_context=_ctx(nearest_resistance=22500.0, nearest_resistance_label="R1"),
        )
        assert should_exit is False           # no exit — trail engaged
        assert reason == ""
        assert new_milestone == 1             # trail flag activated

    def test_ce_no_engagement_when_not_in_profit(self):
        pos = _pos(side="BUY", entry_option_price=100.0)
        _, _, _, new_milestone = check_exit(
            pos, underlying_ltp=22500.0, option_ltp=95.0, greeks=_greeks(),
            now=_now(11, 0),
            market_context=_ctx(nearest_resistance=22500.0, nearest_resistance_label="R1"),
        )
        assert new_milestone == 0             # not in profit → no engagement

    def test_ce_no_engagement_when_underlying_far_from_level(self):
        pos = _pos(side="BUY", entry_option_price=100.0)
        # Use a smaller premium gain (+10%) to stay below the +15% TRENDING
        # milestone — otherwise the milestone fires and confounds the assertion.
        _, _, _, new_milestone = check_exit(
            pos, underlying_ltp=22400.0, option_ltp=110.0, greeks=_greeks(),
            now=_now(11, 0),
            market_context=_ctx(nearest_resistance=22500.0, nearest_resistance_label="R1"),
        )
        # 22400 is ~0.44% below 22500 — outside the 0.25% proximity band
        assert new_milestone == 0

    # ── PE side: PA_SUPPORT engages trail ──
    def test_pe_engages_trail_when_near_support_in_profit(self):
        """SELL/PE with underlying right at support and option up → trail engaged."""
        pos = _pos(side="SELL", entry_option_price=100.0,
                   option_symbol="NSE:NIFTY2640322000PE")
        should_exit, reason, _, new_milestone = check_exit(
            pos, underlying_ltp=22000.0, option_ltp=120.0, greeks=_greeks(),
            now=_now(11, 0),
            market_context=_ctx(nearest_support=22000.0, nearest_support_label="S1"),
        )
        assert should_exit is False
        assert reason == ""
        assert new_milestone == 1

    def test_pe_engages_via_pdl_when_underlying_above_pdl(self):
        """PDL only counts as a support level when underlying is still above it."""
        pos = _pos(side="SELL", entry_option_price=100.0,
                   option_symbol="NSE:NIFTY2640322000PE")
        _, _, _, new_milestone = check_exit(
            pos, underlying_ltp=22050.0, option_ltp=120.0, greeks=_greeks(),
            now=_now(11, 0),
            market_context=_ctx(prev_day_low=22000.0, nearest_support=21000.0),
        )
        assert new_milestone == 1             # PDL @ 22000, underlying 22050 within 0.25%

    def test_pe_no_engagement_when_pdl_below_and_already_broken(self):
        """If underlying has broken below PDL, PDL is excluded as a target."""
        pos = _pos(side="SELL", entry_option_price=100.0,
                   option_symbol="NSE:NIFTY2640322000PE")
        # Use +10% premium gain to stay below the TRENDING +15% milestone.
        _, _, _, new_milestone = check_exit(
            pos, underlying_ltp=21900.0, option_ltp=110.0, greeks=_greeks(),
            now=_now(11, 0),
            market_context=_ctx(prev_day_low=22000.0, nearest_support=21000.0),
        )
        # PDL skipped; nearest_support 21000 too far → no engagement
        assert new_milestone == 0

    # ── Milestone idempotency: don't re-engage if already on trail ──
    def test_no_re_engagement_when_milestone_already_set(self):
        """Once milestone > 0 (trail already active via milestone hit), PA stays quiet."""
        pos = _pos(side="BUY", entry_option_price=100.0, milestone_count=1,
                   peak_option_price=120.0)
        # peak below trail floor would exit; pick option_ltp that doesn't trigger trail
        should_exit, reason, _, new_milestone = check_exit(
            pos, underlying_ltp=22500.0, option_ltp=120.0, greeks=_greeks(),
            now=_now(11, 0),
            market_context=_ctx(nearest_resistance=22500.0, nearest_resistance_label="R1"),
        )
        assert new_milestone == 1             # unchanged; PA did not re-fire

    # ── Minimum gross gain — guard against commission-bleeding engagements ──
    def test_no_engagement_when_gross_gain_below_minimum(self):
        """Option just barely above entry — gross too small to justify trail."""
        from execution.exit_rules import PA_MIN_GROSS_PROFIT_PER_LOT
        pos = _pos(side="BUY", entry_option_price=100.0)
        # qty=50, 1 lot → minimum gross = PA_MIN * 1 = ₹5.0
        # option 100.05 → gain 0.05 × 50 = ₹2.5 (below ₹5)
        _, _, _, new_milestone = check_exit(
            pos, underlying_ltp=22500.0, option_ltp=100.05, greeks=_greeks(),
            now=_now(11, 0),
            market_context=_ctx(nearest_resistance=22500.0, nearest_resistance_label="R1"),
        )
        assert new_milestone == 0

    # ── End-to-end: PA engages trail, then TRAIL_FLOOR fires on retracement ──
    def test_engaged_trail_then_floor_exits_on_retracement(self):
        """Step 1 — PA engagement sets milestone=1.
        Step 2 — next tick with option premium below peak × 0.95 → TRAIL_STOP."""
        # Tick 1: option at peak (120), trail engages
        pos = _pos(side="BUY", entry_option_price=100.0)
        should_exit, _, _, new_milestone = check_exit(
            pos, underlying_ltp=22500.0, option_ltp=120.0, greeks=_greeks(),
            now=_now(11, 0),
            market_context=_ctx(nearest_resistance=22500.0, nearest_resistance_label="R1"),
        )
        assert should_exit is False and new_milestone == 1

        # Tick 2: simulate the position-watcher state mutation
        pos.milestone_count = 1
        pos.peak_option_price = 120.0
        # Option retraces to 113.50 = 120 × (1 - 5%) - small slip → TRAIL_STOP fires
        should_exit, reason, exit_px, _ = check_exit(
            pos, underlying_ltp=22500.0, option_ltp=113.50, greeks=_greeks(),
            now=_now(11, 1),
            market_context=_ctx(nearest_resistance=22500.0, nearest_resistance_label="R1"),
        )
        assert should_exit
        assert reason == "TRAIL_STOP"
        assert exit_px == 113.50


# ── Rule 4: Delta erosion ─────────────────────────────────────────────────────

class TestDeltaErosion:
    def test_triggers_below_threshold(self):
        should_exit, reason, _, _ = check_exit(
            _pos(), underlying_ltp=22150.0, option_ltp=91.0,
            greeks=_greeks(delta=DELTA_EROSION_MIN - 0.01),
            now=_now(10, 0),
        )
        assert should_exit
        assert reason == "DELTA_ERODED"

    def test_does_not_trigger_at_threshold(self):
        should_exit, _, _, _ = check_exit(
            _pos(), underlying_ltp=22150.0, option_ltp=91.0,
            greeks=_greeks(delta=DELTA_EROSION_MIN),
            now=_now(10, 0),
        )
        assert not should_exit

    def test_does_not_trigger_when_greeks_missing(self):
        should_exit, _, _, _ = check_exit(
            _pos(), underlying_ltp=22150.0, option_ltp=91.0, greeks=None,
            now=_now(10, 0),
        )
        assert not should_exit

    def test_does_not_trigger_when_delta_zero(self):
        # delta=0 treated as missing data → default 1.0 → no trigger
        should_exit, _, _, _ = check_exit(
            _pos(), underlying_ltp=22150.0, option_ltp=91.0,
            greeks={"delta": 0, "iv": 18.0},
            now=_now(10, 0),
        )
        assert not should_exit


# ── Rule 5: IV crush ──────────────────────────────────────────────────────────

class TestIVCrush:
    def test_triggers_when_iv_drops_below_threshold(self):
        entry_iv = 20.0
        crushed_iv = entry_iv * IV_CRUSH_THRESHOLD - 0.1
        should_exit, reason, _, _ = check_exit(
            _pos(entry_iv=entry_iv), underlying_ltp=22150.0, option_ltp=91.0,
            greeks=_greeks(iv=crushed_iv), now=_now(10, 0),
        )
        assert should_exit
        assert reason == "IV_CRUSH"

    def test_does_not_trigger_at_threshold(self):
        entry_iv = 20.0
        should_exit, _, _, _ = check_exit(
            _pos(entry_iv=entry_iv), underlying_ltp=22150.0, option_ltp=91.0,
            greeks=_greeks(iv=entry_iv * IV_CRUSH_THRESHOLD),
            now=_now(10, 0),
        )
        assert not should_exit

    def test_skipped_when_entry_iv_zero(self):
        should_exit, _, _, _ = check_exit(
            _pos(entry_iv=0.0), underlying_ltp=22150.0, option_ltp=91.0,
            greeks=_greeks(iv=5.0), now=_now(10, 0),
        )
        assert not should_exit

    def test_skipped_when_current_iv_zero(self):
        should_exit, _, _, _ = check_exit(
            _pos(entry_iv=20.0), underlying_ltp=22150.0, option_ltp=91.0,
            greeks=_greeks(iv=0.0), now=_now(10, 0),
        )
        assert not should_exit


# ── Premium-gain trail trigger ────────────────────────────────────────────────
# Engages the trail at +5% peak gain (before the +15% milestone), with a
# variable offset so trail floor always sits at-or-above entry. Closes the
# gap where positions ride +5%-+14% with no protective mechanism active.

class TestPremiumGainTrailTrigger:

    def test_engages_at_plus_5_pct_peak_gain(self):
        """Peak gain just at +5% threshold engages trail (milestone → 1)."""
        pos = _pos(entry_option_price=100.0, peak_option_price=105.0)
        should_exit, _, _, new_milestone = check_exit(
            pos, underlying_ltp=22500.0, option_ltp=104.0, greeks=_greeks(),
            now=_now(11, 0),
        )
        assert should_exit is False
        assert new_milestone == 1

    def test_no_engagement_below_5_pct_peak(self):
        pos = _pos(entry_option_price=100.0, peak_option_price=104.9)
        _, _, _, new_milestone = check_exit(
            pos, underlying_ltp=22500.0, option_ltp=104.0, greeks=_greeks(),
            now=_now(11, 0),
        )
        assert new_milestone == 0

    def test_no_re_engagement_when_milestone_already_set(self):
        """If milestone > 0, the premium-gain trigger stays quiet."""
        pos = _pos(entry_option_price=100.0, peak_option_price=110.0, milestone_count=1)
        _, _, _, new_milestone = check_exit(
            pos, underlying_ltp=22500.0, option_ltp=108.0, greeks=_greeks(),
            now=_now(11, 0),
        )
        assert new_milestone == 1   # unchanged

    def test_variable_offset_at_plus_5_pct_uses_3_pct_offset(self):
        """At +5% peak gain, offset is 3% → floor = peak × 0.97 (above entry)."""
        # peak=105, entry=100 → +5% → 3% offset → floor=101.85 (above entry)
        # Set milestone=1 (already engaged) so TRAIL_FLOOR rule evaluates.
        pos = _pos(entry_option_price=100.0, peak_option_price=105.0, milestone_count=1)
        # option_ltp at 101.50 < floor 101.85 → TRAIL_STOP fires
        should_exit, reason, exit_px, _ = check_exit(
            pos, underlying_ltp=22500.0, option_ltp=101.50, greeks=_greeks(),
            now=_now(11, 0),
        )
        assert should_exit
        assert reason == "TRAIL_STOP"
        assert exit_px == 101.50

    def test_variable_offset_at_plus_10_pct_uses_5_pct(self):
        """At +10% peak, offset is back to 5% → floor = peak × 0.95."""
        pos = _pos(entry_option_price=100.0, peak_option_price=110.0, milestone_count=1)
        # floor = 110 × 0.95 = 104.50
        # option at 104.49 → fires
        should_exit, reason, _, _ = check_exit(
            pos, underlying_ltp=22500.0, option_ltp=104.49, greeks=_greeks(),
            now=_now(11, 0),
        )
        assert should_exit
        assert reason == "TRAIL_STOP"

    def test_variable_offset_at_plus_7_pct_uses_4_pct(self):
        """At +7% peak, offset is 4% → floor = peak × 0.96."""
        pos = _pos(entry_option_price=100.0, peak_option_price=107.0, milestone_count=1)
        # floor = 107 × 0.96 = 102.72
        should_exit, reason, _, _ = check_exit(
            pos, underlying_ltp=22500.0, option_ltp=102.71, greeks=_greeks(),
            now=_now(11, 0),
        )
        assert should_exit
        assert reason == "TRAIL_STOP"

    def test_trail_floor_stays_above_entry_at_5_pct_peak(self):
        """The bug this rule fixes: at exactly +5% peak with the old 5%
        offset, trail floor would be 99.75 (below entry). With variable
        offset (3% at +5%), floor is 101.85 — above entry. Verify."""
        pos = _pos(entry_option_price=100.0, peak_option_price=105.0, milestone_count=1)
        # option just barely above floor 101.85 → no exit
        should_exit, _, _, _ = check_exit(
            pos, underlying_ltp=22500.0, option_ltp=102.0, greeks=_greeks(),
            now=_now(11, 0),
        )
        assert should_exit is False


# ── Rule 6: Trail floor ───────────────────────────────────────────────────────

class TestTrailFloor:
    """
    Trail is only active once milestone_count > 0.
    Floor = peak_option_price × (1 − TRAIL_OFFSET_PCT)
    e.g. entry=₹100, peak=₹130, floor=₹130 × 0.95 = ₹123.50
    """

    def test_triggers_below_trail_floor(self):
        entry = 100.0
        peak  = 130.0   # +30% from entry
        floor = peak * (1 - TRAIL_OFFSET_PCT)  # ₹123.50
        below_floor = floor - 0.01
        should_exit, reason, _, _ = check_exit(
            _pos(entry_option_price=entry, peak_option_price=peak, milestone_count=1),
            underlying_ltp=22150.0, option_ltp=below_floor, greeks=_greeks(),
            now=_now(11, 0),
        )
        assert should_exit
        assert reason == "TRAIL_STOP"

    def test_does_not_trigger_above_trail_floor(self):
        entry = 100.0
        peak  = 130.0
        floor = peak * (1 - TRAIL_OFFSET_PCT)  # ₹123.50
        above_floor = floor + 0.01
        should_exit, _, _, _ = check_exit(
            _pos(entry_option_price=entry, peak_option_price=peak, milestone_count=1),
            underlying_ltp=22150.0, option_ltp=above_floor, greeks=_greeks(),
            now=_now(11, 0),
        )
        assert not should_exit

    def test_does_not_trigger_before_trail_engaged(self):
        """milestone_count=0 means trail not yet active — no trail floor check.
        Use option_ltp below the trail floor but also below first milestone
        so neither trail nor milestone fires — only the SL range matters."""
        entry = 100.0
        peak  = 130.0
        # option_ltp=₹95: above SL floor (₹90), below trail floor (₹123.50),
        # and below first milestone (₹120) — should produce no exit.
        should_exit, _, _, _ = check_exit(
            _pos(entry_option_price=entry, peak_option_price=peak, milestone_count=0),
            underlying_ltp=22150.0, option_ltp=95.0, greeks=_greeks(),
            now=_now(11, 0),
        )
        assert not should_exit

    def test_exit_price_is_option_ltp(self):
        entry = 100.0
        peak  = 130.0
        below_floor = peak * (1 - TRAIL_OFFSET_PCT) - 1.0
        _, _, exit_price, _ = check_exit(
            _pos(entry_option_price=entry, peak_option_price=peak, milestone_count=1),
            underlying_ltp=22150.0, option_ltp=below_floor, greeks=_greeks(),
            now=_now(11, 0),
        )
        assert exit_price == below_floor


# ── Rule 7: Milestone checks ──────────────────────────────────────────────────

class TestMilestone:
    """
    First target: entry × (1 + 20%) = entry × 1.20
    With confirmed indicators → no exit, milestone_count increments.
    With unconfirmed indicators → exit CLOSED at milestone price.
    """

    def test_first_milestone_confirmed_does_not_exit(self):
        entry = 100.0
        at_target = entry * (1 + FIRST_MILESTONE_PCT)  # ₹120
        should_exit, _, _, new_ms = check_exit(
            _pos(entry_option_price=entry, milestone_count=0),
            underlying_ltp=22500.0, option_ltp=at_target, greeks=_greeks(),
            indicators=_indicators_bullish(), now=_now(11, 0),
        )
        assert not should_exit
        assert new_ms == 1

    def test_first_milestone_not_confirmed_exits(self):
        entry = 100.0
        at_target = entry * (1 + FIRST_MILESTONE_PCT)  # ₹120
        should_exit, reason, exit_price, new_ms = check_exit(
            _pos(entry_option_price=entry, milestone_count=0),
            underlying_ltp=22500.0, option_ltp=at_target, greeks=_greeks(),
            indicators=_indicators_neutral(), now=_now(11, 0),
        )
        assert should_exit
        assert reason == "CLOSED"
        assert exit_price == at_target
        assert new_ms == 1

    def test_second_milestone_confirmed_does_not_exit(self):
        """milestone_count=1 → next target is entry + 20% + 10% = entry × 1.30."""
        entry = 100.0
        at_second = entry * (1 + FIRST_MILESTONE_PCT + MILESTONE_STEP_PCT)  # ₹130
        should_exit, _, _, new_ms = check_exit(
            _pos(entry_option_price=entry, peak_option_price=125.0, milestone_count=1),
            underlying_ltp=22500.0, option_ltp=at_second, greeks=_greeks(),
            indicators=_indicators_bullish(), now=_now(11, 0),
        )
        assert not should_exit
        assert new_ms == 2

    def test_second_milestone_not_confirmed_exits(self):
        entry = 100.0
        at_second = entry * (1 + FIRST_MILESTONE_PCT + MILESTONE_STEP_PCT)  # ₹130
        should_exit, reason, exit_price, _ = check_exit(
            _pos(entry_option_price=entry, peak_option_price=125.0, milestone_count=1),
            underlying_ltp=22500.0, option_ltp=at_second, greeks=_greeks(),
            indicators=_indicators_neutral(), now=_now(11, 0),
        )
        assert should_exit
        assert reason == "CLOSED"
        assert exit_price == at_second

    def test_below_first_milestone_does_not_trigger(self):
        entry = 100.0
        below = entry * 1.10  # +10%, below first milestone of +15%
        should_exit, _, _, ms = check_exit(
            _pos(entry_option_price=entry, milestone_count=0),
            underlying_ltp=22500.0, option_ltp=below, greeks=_greeks(),
            indicators=_indicators_bullish(), now=_now(11, 0),
        )
        assert not should_exit
        assert ms == 0  # unchanged

    def test_pe_sell_confirmed_with_bearish_indicators(self):
        entry = 100.0
        at_target = entry * 1.20
        should_exit, _, _, new_ms = check_exit(
            _pos(side="SELL", entry_option_price=entry, milestone_count=0),
            underlying_ltp=22500.0, option_ltp=at_target, greeks=_greeks(),
            indicators=_indicators_bearish(), now=_now(11, 0),
        )
        assert not should_exit
        assert new_ms == 1

    def test_pe_sell_not_confirmed_with_bullish_indicators(self):
        """Bullish index when holding PE → indicators don't confirm → exit at milestone."""
        entry = 100.0
        at_target = entry * 1.20
        should_exit, reason, _, _ = check_exit(
            _pos(side="SELL", entry_option_price=entry, milestone_count=0),
            underlying_ltp=22500.0, option_ltp=at_target, greeks=_greeks(),
            indicators=_indicators_bullish(), now=_now(11, 0),
        )
        assert should_exit
        assert reason == "CLOSED"


# ── Rules 8 & 9: Non-option (equity / direct index) ──────────────────────────

class TestNonOptionSLTarget:
    """When no option is held, falls back to index-level SL/target (equity trades)."""

    def _equity_pos(self, side="BUY"):
        return Position(
            symbol="NSE:NIFTY50-INDEX",
            side=side,
            quantity=1,
            avg_price=22100.0,
            entry_time=datetime(2026, 4, 7, 9, 30, tzinfo=IST),
            stop_loss=22000.0,
            target=22300.0,
            decision_id="test",
            option_symbol=None,
        )

    def test_buy_stop_loss(self):
        should_exit, reason, exit_price, _ = check_exit(
            self._equity_pos("BUY"), underlying_ltp=21999.0,
            option_ltp=None, greeks=None, now=_now(10, 0),
        )
        assert should_exit
        assert reason == "STOPPED"
        assert exit_price == 21999.0

    def test_buy_target_hit(self):
        should_exit, reason, _, _ = check_exit(
            self._equity_pos("BUY"), underlying_ltp=22300.0,
            option_ltp=None, greeks=None, now=_now(10, 0),
        )
        assert should_exit
        assert reason == "CLOSED"

    def test_sell_stop_loss(self):
        # SELL: stop_loss is above entry — triggered when price rises above it
        pos = Position(
            symbol="NSE:NIFTY50-INDEX", side="SELL", quantity=1,
            avg_price=22100.0, entry_time=datetime(2026, 4, 7, 9, 30, tzinfo=IST),
            stop_loss=22300.0, target=22000.0, decision_id="test", option_symbol=None,
        )
        should_exit, reason, _, _ = check_exit(
            pos, underlying_ltp=22301.0,
            option_ltp=None, greeks=None, now=_now(10, 0),
        )
        assert should_exit
        assert reason == "STOPPED"

    def test_sell_target_hit(self):
        # SELL: target is below entry — triggered when price falls to or below it
        pos = Position(
            symbol="NSE:NIFTY50-INDEX", side="SELL", quantity=1,
            avg_price=22100.0, entry_time=datetime(2026, 4, 7, 9, 30, tzinfo=IST),
            stop_loss=22300.0, target=22000.0, decision_id="test", option_symbol=None,
        )
        should_exit, reason, _, _ = check_exit(
            pos, underlying_ltp=22000.0,
            option_ltp=None, greeks=None, now=_now(10, 0),
        )
        assert should_exit
        assert reason == "CLOSED"

    def test_no_exit_within_range(self):
        should_exit, _, _, _ = check_exit(
            self._equity_pos("BUY"), underlying_ltp=22150.0,
            option_ltp=None, greeks=None, now=_now(10, 0),
        )
        assert not should_exit


# ── Rule priority ─────────────────────────────────────────────────────────────

class TestRulePriority:
    def test_session_close_beats_stop_loss(self):
        should_exit, reason, _, _ = check_exit(
            _pos(entry_option_price=100.0),
            underlying_ltp=22150.0, option_ltp=85.0,  # SL also triggered
            greeks=_greeks(), now=_now(SESSION_CLOSE_HOUR, SESSION_CLOSE_MINUTE),
        )
        assert should_exit
        assert reason == "SESSION_CLOSE"

    def test_stop_loss_beats_delta_eroded(self):
        """STOP_LOSS (rule 2) fires before DELTA_ERODED (rule 3)."""
        should_exit, reason, _, _ = check_exit(
            _pos(entry_option_price=100.0),
            underlying_ltp=22150.0, option_ltp=89.0,  # below SL floor
            greeks=_greeks(delta=0.05),               # delta also eroded
            now=_now(10, 0),
        )
        assert should_exit
        assert reason == "STOP_LOSS"

    def test_delta_eroded_beats_trail(self):
        """DELTA_ERODED (rule 3) fires before TRAIL_FLOOR (rule 5)."""
        entry = 100.0
        peak  = 130.0
        below_floor = peak * (1 - TRAIL_OFFSET_PCT) - 1.0  # trail would trigger
        should_exit, reason, _, _ = check_exit(
            _pos(entry_option_price=entry, peak_option_price=peak, milestone_count=1),
            underlying_ltp=22150.0, option_ltp=below_floor,
            greeks=_greeks(delta=0.05),  # also eroded
            now=_now(10, 0),
        )
        assert should_exit
        assert reason == "DELTA_ERODED"

    def test_no_option_falls_through_to_equity_rules(self):
        """With no option, rules 1–6 skip and equity SL fires."""
        pos = Position(
            symbol="NSE:NIFTY50-INDEX",
            side="BUY",
            quantity=1,
            avg_price=22100.0,
            entry_time=datetime(2026, 4, 7, 9, 30, tzinfo=IST),
            stop_loss=22000.0,
            target=22300.0,
            decision_id="test",
            option_symbol=None,
        )
        should_exit, reason, exit_price, _ = check_exit(
            pos, underlying_ltp=21990.0, option_ltp=None, greeks=None,
            now=_now(10, 0),
        )
        assert should_exit
        assert reason == "STOPPED"
        assert exit_price == 21990.0


# ── Day-type-aware milestone exits ────────────────────────────────────────────

class TestDayTypeExits:
    """
    RANGING day (CPR width ≥ 0.25%): first milestone at +10%, always exit immediately.
    TRENDING day (CPR width < 0.25%): existing +20% milestone with indicator check.
    None day_type: treated as TRENDING (backward-compatible default).
    """

    # ── RANGING: +10% immediate exit ──────────────────────────────────────────

    def test_ranging_exits_at_10pct_milestone(self):
        entry = 100.0
        at_target = entry * (1 + RANGING_MILESTONE_PCT)  # ₹110
        should_exit, reason, exit_price, new_ms = check_exit(
            _pos(entry_option_price=entry, milestone_count=0, day_type="RANGING"),
            underlying_ltp=22500.0, option_ltp=at_target, greeks=_greeks(),
            indicators=_indicators_bullish(), now=_now(11, 0),
        )
        assert should_exit
        assert reason == "CLOSED"
        assert exit_price == at_target
        assert new_ms == 1

    def test_ranging_exits_immediately_without_indicator_check(self):
        """RANGING always exits — indicators are irrelevant."""
        entry = 100.0
        at_target = entry * (1 + RANGING_MILESTONE_PCT)
        # Use neutral indicators (would normally NOT confirm on TRENDING)
        should_exit, reason, _, _ = check_exit(
            _pos(entry_option_price=entry, milestone_count=0, day_type="RANGING"),
            underlying_ltp=22500.0, option_ltp=at_target, greeks=_greeks(),
            indicators=_indicators_neutral(), now=_now(11, 0),
        )
        assert should_exit
        assert reason == "CLOSED"

    def test_ranging_does_not_exit_below_10pct(self):
        """Below +10% on a RANGING day — no milestone exit."""
        entry = 100.0
        below = entry * 1.09  # +9%, below RANGING_MILESTONE_PCT threshold
        should_exit, _, _, ms = check_exit(
            _pos(entry_option_price=entry, milestone_count=0, day_type="RANGING"),
            underlying_ltp=22500.0, option_ltp=below, greeks=_greeks(),
            indicators=_indicators_bullish(), now=_now(11, 0),
        )
        assert not should_exit
        assert ms == 0

    def test_ranging_does_not_exit_at_19pct_which_is_below_trending_milestone(self):
        """RANGING exits at +10%, NOT +20% — verify it fires before TRENDING threshold."""
        entry = 100.0
        at_19pct = entry * 1.19
        should_exit, _, _, _ = check_exit(
            _pos(entry_option_price=entry, milestone_count=0, day_type="RANGING"),
            underlying_ltp=22500.0, option_ltp=at_19pct, greeks=_greeks(),
            indicators=_indicators_bullish(), now=_now(11, 0),
        )
        # +19% > RANGING_MILESTONE_PCT (10%) → should exit
        assert should_exit

    def test_ranging_pe_exits_at_10pct(self):
        entry = 100.0
        at_target = entry * (1 + RANGING_MILESTONE_PCT)
        should_exit, reason, _, _ = check_exit(
            _pos(side="SELL", entry_option_price=entry, milestone_count=0, day_type="RANGING"),
            underlying_ltp=22500.0, option_ltp=at_target, greeks=_greeks(),
            indicators=_indicators_bearish(), now=_now(11, 0),
        )
        assert should_exit
        assert reason == "CLOSED"

    def test_ranging_stop_loss_still_fires_before_milestone(self):
        """Hard stop (−10%) always takes priority even on RANGING days."""
        entry = 100.0
        below_sl = entry * (1 - PREMIUM_SL_PCT)
        should_exit, reason, _, _ = check_exit(
            _pos(entry_option_price=entry, milestone_count=0, day_type="RANGING"),
            underlying_ltp=22500.0, option_ltp=below_sl, greeks=_greeks(),
            now=_now(11, 0),
        )
        assert should_exit
        assert reason == "STOP_LOSS"

    # ── TRENDING: +20% milestone with indicator check ────────────────────────

    def test_trending_does_not_exit_at_10pct_with_confirmed_indicators(self):
        """TRENDING day: +10% is not a milestone — position should stay open."""
        entry = 100.0
        at_10pct = entry * (1 + RANGING_MILESTONE_PCT)  # ₹110 — below TRENDING threshold
        should_exit, _, _, ms = check_exit(
            _pos(entry_option_price=entry, milestone_count=0, day_type="TRENDING"),
            underlying_ltp=22500.0, option_ltp=at_10pct, greeks=_greeks(),
            indicators=_indicators_bullish(), now=_now(11, 0),
        )
        assert not should_exit
        assert ms == 0

    def test_trending_exits_at_20pct_when_not_confirmed(self):
        entry = 100.0
        at_target = entry * (1 + FIRST_MILESTONE_PCT)  # ₹120
        should_exit, reason, exit_price, _ = check_exit(
            _pos(entry_option_price=entry, milestone_count=0, day_type="TRENDING"),
            underlying_ltp=22500.0, option_ltp=at_target, greeks=_greeks(),
            indicators=_indicators_neutral(), now=_now(11, 0),
        )
        assert should_exit
        assert reason == "CLOSED"
        assert exit_price == at_target

    def test_trending_trails_at_20pct_when_confirmed(self):
        entry = 100.0
        at_target = entry * (1 + FIRST_MILESTONE_PCT)
        should_exit, _, _, new_ms = check_exit(
            _pos(entry_option_price=entry, milestone_count=0, day_type="TRENDING"),
            underlying_ltp=22500.0, option_ltp=at_target, greeks=_greeks(),
            indicators=_indicators_bullish(), now=_now(11, 0),
        )
        assert not should_exit
        assert new_ms == 1

    # ── None day_type: backward-compatible (treated as TRENDING) ─────────────

    def test_none_day_type_behaves_as_trending(self):
        """Positions without day_type (e.g. carried over from before this feature)
        use TRENDING behaviour — first milestone at +20%."""
        entry = 100.0
        at_10pct = entry * (1 + RANGING_MILESTONE_PCT)
        should_exit, _, _, ms = check_exit(
            _pos(entry_option_price=entry, milestone_count=0, day_type=None),
            underlying_ltp=22500.0, option_ltp=at_10pct, greeks=_greeks(),
            indicators=_indicators_bullish(), now=_now(11, 0),
        )
        assert not should_exit
        assert ms == 0
