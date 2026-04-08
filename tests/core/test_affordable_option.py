"""
Unit tests for get_affordable_option() in core-engine/fyers/options.py.

All Fyers SDK calls are stubbed — no network, no auth, no Redis.
No test data is written anywhere.
"""

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ── Stub helpers ──────────────────────────────────────────────────────────────

def _stub(name, **attrs):
    m = ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


# Minimal stubs required by options.py imports
_stub("config", settings=SimpleNamespace())
_fyers_auth   = _stub("fyers.auth",        get_fyers_client=MagicMock())
_fyers_market = _stub("fyers.market_data", get_quote=MagicMock(return_value=None))

from fyers.options import get_affordable_option, _lot_size_cache, MAX_OTM_STEPS  # noqa: E402

# ── Test data ─────────────────────────────────────────────────────────────────

UNDERLYING = "NSE:NIFTY50-INDEX"
LTP        = 24010.0   # sits just above 24000 → ATM strike = 24000
LOT_SIZE   = 75

# Simulated Fyers option chain — 4 CE + 4 PE strikes around ATM
_CE_CHAIN = [
    {"strike_price": 24000, "option_type": "CE", "symbol": "NSE:NIFTY24000CE", "ltp": 100.0},
    {"strike_price": 24050, "option_type": "CE", "symbol": "NSE:NIFTY24050CE", "ltp":  75.0},
    {"strike_price": 24100, "option_type": "CE", "symbol": "NSE:NIFTY24100CE", "ltp":  55.0},
    {"strike_price": 24150, "option_type": "CE", "symbol": "NSE:NIFTY24150CE", "ltp":  40.0},
]
_PE_CHAIN = [
    {"strike_price": 24000, "option_type": "PE", "symbol": "NSE:NIFTY24000PE", "ltp":  95.0},
    {"strike_price": 23950, "option_type": "PE", "symbol": "NSE:NIFTY23950PE", "ltp":  70.0},
    {"strike_price": 23900, "option_type": "PE", "symbol": "NSE:NIFTY23900PE", "ltp":  50.0},
    {"strike_price": 23850, "option_type": "PE", "symbol": "NSE:NIFTY23850PE", "ltp":  35.0},
]
_CHAIN      = _CE_CHAIN + _PE_CHAIN
_EXPIRY     = [{"date": "17-04-2026"}]
_CHAIN_RESP = {"data": {"optionsChain": _CHAIN, "expiryData": _EXPIRY}}


def _fyers_mock(chain_resp=_CHAIN_RESP):
    """Return a Fyers client mock with optionchain and depth configured.

    depth is keyed by the symbol passed to the call so it works for both
    the CE and PE ATM symbols.
    """
    fyers = MagicMock()
    fyers.optionchain.return_value = chain_resp

    def _depth(data):
        sym = data.get("symbol", "")
        return {"d": {sym: {"bids": [{"volume": LOT_SIZE}], "ask": [{"volume": LOT_SIZE * 2}]}}}

    fyers.depth.side_effect = _depth
    return fyers


@pytest.fixture(autouse=True)
def clear_lot_cache():
    """Ensure lot size cache doesn't bleed between tests."""
    _lot_size_cache.clear()
    yield
    _lot_size_cache.clear()


def _call(decision: str, max_spend, chain_resp=_CHAIN_RESP):
    """Call get_affordable_option with a stubbed Fyers client.

    Resolves fyers.auth from sys.modules at call time — other test files
    may re-register the stub, so we must not cache the reference at import time.
    """
    live_fyers_auth = sys.modules["fyers.auth"]
    with patch.object(live_fyers_auth, "get_fyers_client", return_value=_fyers_mock(chain_resp)):
        return get_affordable_option(UNDERLYING, LTP, decision, max_spend=max_spend)


# ── CE (BUY) tests ────────────────────────────────────────────────────────────

class TestCEAffordableOption:
    def test_picks_atm_when_affordable(self):
        # ATM CE: 100 × 75 = ₹7,500 — budget ₹8,000 → picks ATM
        result = _call("BUY", max_spend=8_000)
        assert result is not None
        sym, strike, opt_type, expiry, lot_size = result
        assert strike == 24000
        assert opt_type == "CE"
        assert expiry == "2026-04-17"
        assert lot_size == LOT_SIZE

    def test_walks_to_otm1_when_atm_too_expensive(self):
        # ATM: 100×75=7500 > 6000; OTM+1 (24050): 75×75=5625 ≤ 6000
        result = _call("BUY", max_spend=6_000)
        assert result is not None
        _, strike, _, _, _ = result
        assert strike == 24050

    def test_walks_to_otm2_when_otm1_also_too_expensive(self):
        # ATM: 7500 > 5000; OTM+1: 5625 > 5000; OTM+2 (24100): 55×75=4125 ≤ 5000
        result = _call("BUY", max_spend=5_000)
        assert result is not None
        _, strike, _, _, _ = result
        assert strike == 24100

    def test_returns_none_when_nothing_affordable_within_steps(self):
        # MAX_OTM_STEPS=3: cheapest candidate is OTM+3 (24150): 40×75=3000 > 2000
        result = _call("BUY", max_spend=2_000)
        assert result is None

    def test_no_budget_constraint_always_picks_atm(self):
        # max_spend=None → no filter → ATM always returned
        result = _call("BUY", max_spend=None)
        assert result is not None
        _, strike, _, _, _ = result
        assert strike == 24000

    def test_walks_higher_strikes_for_ce(self):
        # CE OTM = higher strikes (calls are OTM above current price)
        result = _call("BUY", max_spend=4_500)
        assert result is not None
        _, strike, _, _, _ = result
        assert strike > 24000

    def test_exactly_at_budget_boundary_is_affordable(self):
        # ATM CE: 100×75=7500 exactly equals budget → should be selected
        result = _call("BUY", max_spend=7_500)
        assert result is not None
        _, strike, _, _, _ = result
        assert strike == 24000


# ── PE (SELL) tests ───────────────────────────────────────────────────────────

class TestPEAffordableOption:
    def test_picks_atm_when_affordable(self):
        # ATM PE: 95×75=7125 ≤ 8000 → picks ATM
        result = _call("SELL", max_spend=8_000)
        assert result is not None
        _, strike, opt_type, _, _ = result
        assert strike == 24000
        assert opt_type == "PE"

    def test_walks_to_otm1_when_atm_too_expensive(self):
        # ATM: 7125 > 6000; OTM+1 (23950): 70×75=5250 ≤ 6000
        result = _call("SELL", max_spend=6_000)
        assert result is not None
        _, strike, _, _, _ = result
        assert strike == 23950

    def test_walks_lower_strikes_for_pe(self):
        # PE OTM = lower strikes (puts are OTM below current price)
        result = _call("SELL", max_spend=5_500)
        assert result is not None
        _, strike, _, _, _ = result
        assert strike < 24000

    def test_returns_none_when_nothing_affordable_within_steps(self):
        # Cheapest within MAX_OTM_STEPS is OTM+3 (23850): 35×75=2625 > 2000
        result = _call("SELL", max_spend=2_000)
        assert result is None

    def test_no_budget_constraint_always_picks_atm(self):
        result = _call("SELL", max_spend=None)
        assert result is not None
        _, strike, _, _, _ = result
        assert strike == 24000


# ── Edge cases ────────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_unsupported_underlying_returns_none(self):
        result = get_affordable_option("NSE:UNKNOWN-INDEX", LTP, "BUY", max_spend=10_000)
        assert result is None

    def test_empty_chain_returns_none(self):
        empty_resp = {"data": {"optionsChain": [], "expiryData": _EXPIRY}}
        result = _call("BUY", max_spend=10_000, chain_resp=empty_resp)
        assert result is None

    def test_fyers_api_error_returns_none(self):
        fyers = MagicMock()
        fyers.optionchain.side_effect = Exception("API timeout")
        with patch.object(sys.modules["fyers.auth"], "get_fyers_client", return_value=fyers):
            result = get_affordable_option(UNDERLYING, LTP, "BUY", max_spend=10_000)
        assert result is None

    def test_lot_size_cached_after_first_call(self):
        assert UNDERLYING not in _lot_size_cache
        _call("BUY", max_spend=10_000)
        assert UNDERLYING in _lot_size_cache
        assert _lot_size_cache[UNDERLYING] == LOT_SIZE

    def test_max_otm_steps_constant_is_3(self):
        assert MAX_OTM_STEPS == 3
