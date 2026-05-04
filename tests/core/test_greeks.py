"""
Unit tests for core-engine/fyers/greeks.py

Fyers SDK and auth module are fully mocked — no network calls, no credentials,
no extra pip packages needed.  No test data is written anywhere.
"""

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# ── Stub fyers.auth so greeks.py can be imported without Fyers SDK ────────────
# Only mock the auth submodule; let Python resolve fyers/ as a real package
# from core-engine/ (added to sys.path by conftest.py).
_mock_client = MagicMock()
_mock_auth = ModuleType("fyers.auth")
_mock_auth.get_fyers_client = MagicMock(return_value=_mock_client)
sys.modules["fyers.auth"] = _mock_auth

from fyers.greeks import get_option_quote_with_greeks  # noqa: E402

OPTION_SYMBOL = "NSE:NIFTY2640322200CE"


def _fyers_ok_response(ltp=250.0, delta=0.55, theta=-1.2, vega=15.0, gamma=0.002, iv=18.5):
    return {
        "s": "ok",
        "d": [{"v": {"lp": ltp, "delta": delta, "theta": theta,
                      "vega": vega, "gamma": gamma, "iv": iv}}],
    }


def _set_quote(response):
    """Configure the shared mock client to return the given quotes response."""
    _mock_auth.get_fyers_client.return_value.quotes.return_value = response


class TestGetOptionQuoteWithGreeks:
    def test_returns_all_fields_on_success(self):
        # Greeks are computed via Black-Scholes, not read from the API response.
        # Use side_effect to return different responses for the two quotes calls:
        # first = option LTP, second = underlying spot (ATM ≈ strike 22200).
        option_resp = {"s": "ok", "d": [{"v": {"lp": 250.0}}]}
        spot_resp   = {"s": "ok", "d": [{"v": {"lp": 22200.0}}]}
        _mock_auth.get_fyers_client.return_value.quotes.side_effect = [
            option_resp, spot_resp,
        ]
        result = get_option_quote_with_greeks(OPTION_SYMBOL)
        _mock_auth.get_fyers_client.return_value.quotes.side_effect = None
        assert result is not None
        assert result["symbol"] == OPTION_SYMBOL
        assert result["ltp"]    == 250.0
        assert 0 < result["delta"] < 1   # ATM CE delta near 0.5
        assert result["theta"] < 0       # time decay always negative
        assert result["vega"]  > 0       # vega always positive
        assert result["gamma"] > 0       # gamma always positive
        assert result["iv"]    > 0       # BS should converge for ATM option

    def test_returns_none_when_api_returns_error(self):
        _set_quote({"s": "error", "message": "Invalid symbol"})
        assert get_option_quote_with_greeks(OPTION_SYMBOL) is None

    def test_returns_none_when_ltp_is_zero(self):
        _set_quote(_fyers_ok_response(ltp=0))
        assert get_option_quote_with_greeks(OPTION_SYMBOL) is None

    def test_returns_none_on_exception(self):
        _mock_auth.get_fyers_client.return_value.quotes.side_effect = ConnectionError("fail")
        result = get_option_quote_with_greeks(OPTION_SYMBOL)
        assert result is None
        # Reset side_effect so subsequent tests aren't affected
        _mock_auth.get_fyers_client.return_value.quotes.side_effect = None

    def test_defaults_missing_greek_fields_to_zero(self):
        _set_quote({"s": "ok", "d": [{"v": {"lp": 100.0}}]})
        result = get_option_quote_with_greeks(OPTION_SYMBOL)
        assert result is not None
        assert result["ltp"]   == 100.0
        assert result["delta"] == 0.0
        assert result["theta"] == 0.0
        assert result["vega"]  == 0.0
        assert result["gamma"] == 0.0
        assert result["iv"]    == 0.0

    def test_returns_float_types(self):
        _set_quote(_fyers_ok_response(ltp="250", delta="0.55"))
        result = get_option_quote_with_greeks(OPTION_SYMBOL)
        assert isinstance(result["ltp"], float)
        assert isinstance(result["delta"], float)
