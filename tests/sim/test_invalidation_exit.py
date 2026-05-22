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

# Evict any stubs other test files may have installed for execution.* — we
# need the real invalidation_exit module here. (Gate test files _stub it at
# their import time, which leaks via sys.modules.)
import sys as _sys
for _k in ("execution.invalidation_exit", "execution"):
    _sys.modules.pop(_k, None)

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


# ── Bug-fix coverage (2026-05-20): the new CPR gate allows trades on the
#    "wrong" side of CPR (SELL above, BUY below). The capture step in
#    main.py now filters out levels that are not adverse to the trade
#    direction. The exit check must not iterate hardcoded keys — it must
#    iterate whatever was captured, and a position with NO captured levels
#    (empty / None) must never auto-exit. ────────────────────────────────


class TestUniformLevelHandling:
    """All levels are treated the same — CPR's TC and BC are no different
    from VWAP or EMA21."""

    def test_sell_with_cpr_bc_above_price_invalidates_via_cpr_bc(self):
        """A SELL captured cpr_bc (because it was above entry price).
        Price crossing UP through cpr_bc must trigger invalidation just
        like any other level."""
        pos = _pos("SELL", {"cpr_bc": 53_600.0})
        assert check_invalidation_exit(pos, 53_601.0) == "INVALIDATION_CPR_BC"

    def test_buy_with_cpr_tc_below_price_invalidates_via_cpr_tc(self):
        """A BUY captured cpr_tc (because it was below entry price).
        Price crossing DOWN through cpr_tc must trigger invalidation."""
        pos = _pos("BUY", {"cpr_tc": 23_700.0})
        assert check_invalidation_exit(pos, 23_699.0) == "INVALIDATION_CPR_TC"

    def test_sell_only_vwap_captured_only_vwap_can_invalidate(self):
        """If capture stage decided only VWAP was adverse, only VWAP
        invalidation is possible — no hardcoded list of 'expected' keys."""
        pos = _pos("SELL", {"vwap": 53_589.0})
        assert check_invalidation_exit(pos, 53_400.0) is None
        assert check_invalidation_exit(pos, 53_590.0) == "INVALIDATION_VWAP"

    def test_position_with_no_adverse_levels_never_invalidates(self):
        """A trade taken in a position where no nearby level is adverse
        (capture returned None) relies on premium SL alone — invalidation
        check must always return None."""
        pos_sell = _pos("SELL", None)
        pos_buy  = _pos("BUY",  None)
        for price in (10_000.0, 53_500.0, 100_000.0):
            assert check_invalidation_exit(pos_sell, price) is None
            assert check_invalidation_exit(pos_buy,  price) is None


# ── Capture-side (main._build_invalidation_levels): only levels ADVERSE to
#    the trade direction at entry get stored. Direct unit tests on the pure
#    helper. ────────────────────────────────────────────────────────────────


class TestInvalidationCapture:
    """All four levels (vwap, ema_21, cpr_tc, cpr_bc) are filtered uniformly
    — captured only when adverse to the trade direction at entry time."""

    @staticmethod
    def _build():
        # Evict any stub installed by earlier-running gate test files (which
        # _stub('execution.invalidation_exit', ...) at import time with a
        # MagicMock for build_invalidation_levels). We need the real helper.
        import sys
        for k in ("execution.invalidation_exit", "execution"):
            sys.modules.pop(k, None)
        from execution.invalidation_exit import build_invalidation_levels
        return build_invalidation_levels

    def test_sell_below_all_levels_captures_all(self):
        """Classic SELL setup — all levels are above entry price."""
        build = self._build()
        ind = {"vwap": 53589.0, "ema_21": 53468.0, "cpr_tc": 53520.0, "cpr_bc": 53400.0}
        result = build("SELL", 53341.0, ind)
        # cpr_bc 53400 > 53341 → also captured (no special treatment for CPR)
        assert result == {"vwap": 53589.0, "ema_21": 53468.0, "cpr_tc": 53520.0, "cpr_bc": 53400.0}

    def test_sell_above_all_levels_captures_none(self):
        """SELL above CPR (new gate allows). No level is adverse — returns None."""
        build = self._build()
        ind = {"vwap": 23700.0, "ema_21": 23710.0, "cpr_tc": 23720.0, "cpr_bc": 23680.0}
        result = build("SELL", 23800.0, ind)
        assert result is None

    def test_sell_between_levels_captures_only_those_above(self):
        """SELL with mixed level positions — only adverse subset captured."""
        build = self._build()
        # Entry 23530: VWAP 23528 BELOW (not adverse), EMA21 23540 ABOVE,
        # CPR-TC 23685 ABOVE, CPR-BC 23510 BELOW (not adverse).
        ind = {"vwap": 23528.0, "ema_21": 23540.0, "cpr_tc": 23685.0, "cpr_bc": 23510.0}
        result = build("SELL", 23530.0, ind)
        assert result == {"ema_21": 23540.0, "cpr_tc": 23685.0}

    def test_buy_above_all_levels_captures_all(self):
        """Classic BUY breakout setup — all levels below entry."""
        build = self._build()
        ind = {"vwap": 23800.0, "ema_21": 23850.0, "cpr_tc": 23720.0, "cpr_bc": 23670.0}
        result = build("BUY", 23900.0, ind)
        # All four are below 23900 — all captured.
        assert result == {"vwap": 23800.0, "ema_21": 23850.0, "cpr_tc": 23720.0, "cpr_bc": 23670.0}

    def test_buy_below_all_levels_captures_none(self):
        """BUY below CPR (new gate allows). No level is adverse — None.
        Specifically prevents the 'instant CPR_BC invalidation' bug."""
        build = self._build()
        ind = {"vwap": 23650.0, "ema_21": 23625.0, "cpr_tc": 23720.0, "cpr_bc": 23670.0}
        result = build("BUY", 23613.0, ind)
        assert result is None

    def test_buy_between_levels_captures_only_those_below(self):
        build = self._build()
        # Entry 23615: VWAP 23600 BELOW, EMA21 23620 ABOVE (not adverse),
        # CPR-TC 23700 ABOVE (not adverse), CPR-BC 23590 BELOW.
        ind = {"vwap": 23600.0, "ema_21": 23620.0, "cpr_tc": 23700.0, "cpr_bc": 23590.0}
        result = build("BUY", 23615.0, ind)
        assert result == {"vwap": 23600.0, "cpr_bc": 23590.0}

    def test_hold_captures_none(self):
        build = self._build()
        ind = {"vwap": 23528.0, "ema_21": 23540.0, "cpr_tc": 23685.0, "cpr_bc": 23510.0}
        assert build("HOLD", 23530.0, ind) is None

    def test_missing_levels_treated_as_absent(self):
        build = self._build()
        # Only vwap present.
        ind = {"vwap": 23700.0}
        result = build("SELL", 23600.0, ind)
        assert result == {"vwap": 23700.0}

    def test_zero_levels_treated_as_absent(self):
        """A level set to 0 from the indicators_snapshot must be filtered out
        before the adverse-direction check, not stored as a zero level."""
        build = self._build()
        ind = {"vwap": 0, "ema_21": 23540.0, "cpr_tc": 0, "cpr_bc": 0}
        result = build("SELL", 23530.0, ind)
        # 0 is filtered by `v is not None and v > current_price` — 0 is not > 23530.
        assert result == {"ema_21": 23540.0}

    def test_non_numeric_levels_safely_skipped(self):
        build = self._build()
        ind = {"vwap": "not a number", "ema_21": 23540.0, "cpr_tc": None, "cpr_bc": ""}
        result = build("SELL", 23530.0, ind)
        assert result == {"ema_21": 23540.0}

    def test_the_2026_05_18_case_captures_none(self):
        """2026-05-18 13:14 NIFTY BUY @ 23613, CPR-BC ~23670 overhead.
        Under the OLD capture this captured cpr_bc and fired instant
        invalidation. Under the new capture, returns None."""
        build = self._build()
        ind = {"vwap": 23650.0, "ema_21": 23625.0, "cpr_tc": 23724.80, "cpr_bc": 23670.60}
        assert build("BUY", 23613.0, ind) is None


# ── Cross-symbol invalidation (peer-index sympathy check) ────────────────────
# NIFTY is the leading indicator; BANKNIFTY follows. When holding a BANKNIFTY
# position, we capture NIFTY's adverse VWAP/EMA-21 levels and treat a NIFTY
# cross-back-through as a sympathy invalidation for the BANKNIFTY position.


class TestCrossSymbolCapture:

    @staticmethod
    def _build():
        import sys
        for k in ("execution.invalidation_exit", "execution"):
            sys.modules.pop(k, None)
        from execution.invalidation_exit import build_cross_symbol_invalidation_levels
        return build_cross_symbol_invalidation_levels

    def test_sell_captures_peer_levels_above_peer_price(self):
        """For BANKNIFTY SELL: NIFTY peer levels ABOVE NIFTY price are adverse."""
        build = self._build()
        # NIFTY at 23500, VWAP 23600, EMA21 23550 — both above
        peer_ind = {"vwap": 23600.0, "ema_21": 23550.0}
        assert build("SELL", 23500.0, peer_ind) == {"vwap": 23600.0, "ema_21": 23550.0}

    def test_buy_captures_peer_levels_below_peer_price(self):
        build = self._build()
        # NIFTY at 23700, VWAP 23600, EMA21 23650 — both below
        peer_ind = {"vwap": 23600.0, "ema_21": 23650.0}
        assert build("BUY", 23700.0, peer_ind) == {"vwap": 23600.0, "ema_21": 23650.0}

    def test_sell_skips_non_adverse_peer_levels(self):
        build = self._build()
        # NIFTY at 23700: VWAP 23600 (below, not adverse), EMA21 23800 (above)
        peer_ind = {"vwap": 23600.0, "ema_21": 23800.0}
        assert build("SELL", 23700.0, peer_ind) == {"ema_21": 23800.0}

    def test_no_levels_when_peer_far_past_all(self):
        build = self._build()
        # NIFTY way above all candidates → empty for SELL
        peer_ind = {"vwap": 23500.0, "ema_21": 23550.0}
        assert build("SELL", 24000.0, peer_ind) is None

    def test_hold_returns_none(self):
        build = self._build()
        peer_ind = {"vwap": 23600.0, "ema_21": 23550.0}
        assert build("HOLD", 23500.0, peer_ind) is None


class TestCrossSymbolCheck:

    @staticmethod
    def _check():
        import sys
        for k in ("execution.invalidation_exit", "execution"):
            sys.modules.pop(k, None)
        from execution.invalidation_exit import check_cross_symbol_invalidation
        return check_cross_symbol_invalidation

    def test_sell_fires_when_peer_crosses_up_through_vwap(self):
        check = self._check()
        pos = _pos("SELL", levels=None)
        pos.cross_symbol_invalidation_levels = {"vwap": 23600.0, "ema_21": 23550.0}
        # NIFTY price now 23601 — crossed above peer VWAP
        assert check(pos, 23601.0) == "INVALIDATION_PEER_VWAP"

    def test_buy_fires_when_peer_crosses_down_through_ema21(self):
        check = self._check()
        pos = _pos("BUY", levels=None)
        pos.cross_symbol_invalidation_levels = {"vwap": 23700.0, "ema_21": 23650.0}
        # NIFTY price now 23649 — crossed below peer EMA21 (VWAP not crossed; only first match returns)
        result = check(pos, 23649.0)
        assert result in ("INVALIDATION_PEER_VWAP", "INVALIDATION_PEER_EMA_21")

    def test_no_exit_when_peer_levels_not_crossed(self):
        check = self._check()
        pos = _pos("SELL", levels=None)
        pos.cross_symbol_invalidation_levels = {"vwap": 23600.0, "ema_21": 23550.0}
        # NIFTY still below all peer levels — no invalidation
        assert check(pos, 23500.0) is None

    def test_no_exit_when_no_cross_symbol_levels(self):
        check = self._check()
        pos = _pos("SELL", levels=None)
        # cross_symbol_invalidation_levels is None on the Position by default
        assert check(pos, 23900.0) is None

    def test_no_exit_on_zero_peer_ltp(self):
        check = self._check()
        pos = _pos("SELL", levels=None)
        pos.cross_symbol_invalidation_levels = {"vwap": 23600.0}
        assert check(pos, 0.0) is None
        assert check(pos, -1.0) is None
