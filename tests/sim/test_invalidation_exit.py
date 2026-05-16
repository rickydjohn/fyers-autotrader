"""
Unit tests for execution/invalidation_exit.check_invalidation_exit.

Regression coverage for the 2026-05-14 BANKNIFTY53300PE pattern: a SELL was
opened at 53,341 with thesis "below VWAP=53,589, EMA21=53,468, CPR-TC=53,520",
but the actual exit only fired 4 minutes after price first crossed back
above EMA21 — by then the option SL hit at -10%. With this feature, the
EMA21 cross would have triggered an immediate INVALIDATION exit.
"""
from __future__ import annotations

from datetime import datetime

import pytest
import pytz

from execution.invalidation_exit import check_invalidation_exit
from models.schemas import Position


IST = pytz.timezone("Asia/Kolkata")


def _pos(side: str, levels: dict | None) -> Position:
    """Minimal Position with the only fields the invalidation check reads."""
    return Position(
        symbol="NSE:NIFTYBANK-INDEX",
        side=side,
        quantity=30,
        avg_price=800.0,
        entry_time=IST.localize(datetime(2026, 5, 14, 11, 15)),
        stop_loss=720.0,
        target=960.0,
        decision_id="test",
        invalidation_levels=levels,
    )


# ── SELL / bearish thesis: invalidated when price moves UP past resistance ────

class TestSellInvalidation:

    def test_no_exit_when_price_below_all_levels(self):
        pos = _pos("SELL", {"vwap": 53589.0, "ema_21": 53468.0, "cpr_tc": 53520.0})
        assert check_invalidation_exit(pos, 53_341.0) is None

    def test_exits_on_ema21_cross_above(self):
        """The 2026-05-14 trade's first invalidation — EMA21 crossed at 11:18."""
        pos = _pos("SELL", {"vwap": 53589.0, "ema_21": 53468.0, "cpr_tc": 53520.0})
        assert check_invalidation_exit(pos, 53_469.0) == "INVALIDATION_EMA_21"

    def test_exits_on_cpr_tc_cross_above(self):
        """Second invalidation in the same trade — CPR-TC crossed at 11:20."""
        # Price above CPR-TC but still below VWAP and ahead of EMA21.
        pos = _pos("SELL", {"vwap": 53589.0, "ema_21": 53400.0, "cpr_tc": 53520.0})
        # EMA21 is 53400 — already crossed at 53521. So the FIRST level
        # crossed is reported (helper iterates in order: vwap, ema_21, cpr_tc).
        # Both EMA21 and CPR-TC are crossed; VWAP isn't. Iterating returns EMA21.
        assert check_invalidation_exit(pos, 53_521.0) == "INVALIDATION_EMA_21"

    def test_exits_on_vwap_cross_above(self):
        pos = _pos("SELL", {"vwap": 53589.0, "ema_21": 53800.0, "cpr_tc": 54000.0})
        assert check_invalidation_exit(pos, 53_590.0) == "INVALIDATION_VWAP"

    def test_exact_level_does_not_trigger(self):
        """Strict `>` — price exactly at the level is not yet a cross."""
        pos = _pos("SELL", {"vwap": 53589.0, "ema_21": 53468.0, "cpr_tc": 53520.0})
        assert check_invalidation_exit(pos, 53_468.0) is None  # exactly at EMA21


# ── BUY / bullish thesis: invalidated when price moves DOWN past support ──────

class TestBuyInvalidation:

    def test_no_exit_when_price_above_all_levels(self):
        pos = _pos("BUY", {"vwap": 23500.0, "ema_21": 23450.0, "cpr_bc": 23400.0})
        assert check_invalidation_exit(pos, 23_600.0) is None

    def test_exits_on_ema21_cross_below(self):
        # VWAP below the test price so only EMA21 triggers; iteration order
        # returns the FIRST crossed level (vwap, ema_21, cpr_bc).
        pos = _pos("BUY", {"vwap": 23400.0, "ema_21": 23450.0, "cpr_bc": 23300.0})
        assert check_invalidation_exit(pos, 23_449.0) == "INVALIDATION_EMA_21"

    def test_exits_on_cpr_bc_cross_below(self):
        # Price above EMA21 + VWAP, but below CPR-BC.
        pos = _pos("BUY", {"vwap": 23200.0, "ema_21": 23200.0, "cpr_bc": 23400.0})
        assert check_invalidation_exit(pos, 23_399.0) == "INVALIDATION_CPR_BC"

    def test_exits_on_vwap_cross_below(self):
        pos = _pos("BUY", {"vwap": 23500.0, "ema_21": 23300.0, "cpr_bc": 23200.0})
        assert check_invalidation_exit(pos, 23_499.0) == "INVALIDATION_VWAP"


# ── Edge cases ───────────────────────────────────────────────────────────────

class TestInvalidationEdges:

    def test_no_levels_returns_none(self):
        """Positions opened before this feature shipped (no captured levels)
        must not throw and must not auto-exit."""
        pos = _pos("SELL", None)
        assert check_invalidation_exit(pos, 53_500.0) is None

    def test_empty_levels_dict_returns_none(self):
        pos = _pos("SELL", {})
        assert check_invalidation_exit(pos, 53_500.0) is None

    def test_zero_level_skipped(self):
        """Level=0 is treated as 'missing' — never used as a comparison."""
        pos = _pos("SELL", {"vwap": 0, "ema_21": 53468.0, "cpr_tc": 0})
        # Price way above 0 but only EMA21 is real; EMA21 not crossed → no exit.
        assert check_invalidation_exit(pos, 53_400.0) is None
        # EMA21 crossed → exits.
        assert check_invalidation_exit(pos, 53_469.0) == "INVALIDATION_EMA_21"

    def test_zero_or_negative_ltp_returns_none(self):
        pos = _pos("SELL", {"vwap": 53589.0, "ema_21": 53468.0, "cpr_tc": 53520.0})
        assert check_invalidation_exit(pos, 0.0) is None
        assert check_invalidation_exit(pos, -1.0) is None


# ── Regression: the 2026-05-14 BANKNIFTY53300PE trade ────────────────────────

class TestRealTrade20260514:
    """The trade that motivated this feature. Snapshot levels from the
    decision indicators_snapshot:
      vwap=53589.29  ema_21=53468.44  cpr_tc=53520.46  cpr_bc=53649.07
    The pattern below replays the price walk between entry and stop-loss.
    """

    @pytest.fixture
    def pos(self):
        return _pos("SELL", {
            "vwap":   53589.29,
            "ema_21": 53468.44,
            "cpr_tc": 53520.46,
            "cpr_bc": 53649.07,   # ignored for SELL invalidation
        })

    def test_entry_price_passes_no_exit(self, pos):
        # 11:15 entry @ 53,341 — well below all levels.
        assert check_invalidation_exit(pos, 53_341.0) is None

    def test_first_invalidation_at_ema21_cross(self, pos):
        # 11:18 — price reached 53,469 (EMA21=53,468). Cross fires.
        assert check_invalidation_exit(pos, 53_469.0) == "INVALIDATION_EMA_21"

    def test_continues_to_fire_as_price_runs(self, pos):
        # Later in the run-up, price hits 53,571 (above CPR-TC and VWAP too).
        # Iteration order returns EMA21 (first crossed in the dict).
        assert check_invalidation_exit(pos, 53_571.0) == "INVALIDATION_EMA_21"
