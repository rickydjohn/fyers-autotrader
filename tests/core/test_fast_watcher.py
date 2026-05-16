"""
Unit tests for the _fast_position_watcher scheduler job.
All external dependencies stubbed — no Redis, Fyers SDK, or extra packages.
No test data is written anywhere.
"""

import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Stub helpers ──────────────────────────────────────────────────────────────

def _stub(name, **attrs):
    """Register a fake module in sys.modules with the given attributes."""
    m = ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m

_noop = MagicMock(return_value=None)

# redis
_aioredis = _stub("redis.asyncio", Redis=MagicMock())
_stub("redis", asyncio=_aioredis)

# apscheduler
_stub("apscheduler.schedulers.asyncio", AsyncIOScheduler=MagicMock())
_stub("apscheduler.schedulers")
_stub("apscheduler")

# config
_stub("config", settings=SimpleNamespace(
    llm_decision_interval_seconds=60,
    greeks_poll_interval_seconds=5,
    symbols=["NSE:NIFTY50-INDEX"],
    market_open="09:15",
    market_close="15:30",
))

# fyers.*  — also need to stub parent so submodule lookup works
_stub("fyers.auth",    get_fyers_client=MagicMock())
_stub("fyers.market_data",
      get_historical_candles=_noop,
      get_historical_candles_daterange=_noop,
      get_previous_day_ohlc=_noop,
      get_quote=_noop,
      get_sector_breadth=_noop)
_stub("fyers.options", get_atm_option=_noop)
# Note: fyers.greeks is NOT stubbed here — test_greeks.py needs the real module.
# Do NOT stub the fyers parent package — let Python resolve fyers/ as the real
# package from core-engine so submodule imports work correctly.
# Tests patch get_option_quote_with_greeks via patch.object(_jobs, ...) instead.

# indicators
_stub("indicators.cpr",
      calculate_cpr=_noop, get_cpr_signal=_noop)
_stub("indicators.pivots",
      calculate_pivots=_noop, get_nearest_levels=_noop)
_stub("indicators.technicals",
      aggregate_1m_to_5m=_noop,
      calculate_consolidation=_noop, calculate_day_range=_noop,
      calculate_orb=_noop,
      calculate_ema=_noop, calculate_macd=_noop,
      calculate_rsi=_noop, calculate_vwap=_noop,
      format_candles_for_prompt=MagicMock(return_value=""))
_stub("indicators.historical_sr",
      compute_sr_levels=_noop, format_sr_for_prompt=_noop)
_stub("indicators")

# llm / news / context
_stub("llm.decision",    make_decision=_noop)
_stub("llm.prompts",     compute_forming_bar_signal=_noop, format_sector_breadth_block=MagicMock(return_value=""))
_stub("news.scraper",    get_all_news=_noop)
_stub("news.sentiment",  analyze_sentiment=_noop)
_stub("news")
_stub("context.formatter")
_stub("context")
_stub("data_client")

# models.schemas — real core-engine module (conftest put it on path)
import models.schemas as _schemas  # noqa: F401

from scheduler.jobs import _fast_position_watcher  # noqa: E402
import scheduler.jobs as _jobs                      # noqa: E402

# ── Fixtures ──────────────────────────────────────────────────────────────────

UNDERLYING    = "NSE:NIFTY50-INDEX"
OPTION_SYM    = "NSE:NIFTY2640322200CE"
OPEN_POSITION = {"symbol": UNDERLYING, "side": "BUY", "option_symbol": OPTION_SYM}
GOOD_QUOTE    = {"ltp": 22200.0, "high": 22300.0, "low": 22100.0}
GOOD_GREEKS   = {"symbol": OPTION_SYM, "ltp": 250.0, "delta": 0.55,
                 "theta": -1.2, "vega": 15.0, "gamma": 0.002, "iv": 18.5}


def _redis(positions: dict) -> AsyncMock:
    r = AsyncMock()
    r.hgetall.return_value = {k: json.dumps(v) for k, v in positions.items()}
    return r


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_noop_when_market_closed():
    r = _redis({UNDERLYING: OPEN_POSITION})
    with patch.object(_jobs, "_is_market_open", return_value=False):
        await _fast_position_watcher(r)
    r.hgetall.assert_not_called()


@pytest.mark.asyncio
async def test_noop_when_no_positions():
    r = _redis({})
    with patch.object(_jobs, "_is_market_open", return_value=True):
        await _fast_position_watcher(r)
    r.setex.assert_not_called()


@pytest.mark.asyncio
async def test_does_not_refresh_underlying_ltp():
    """Underlying LTP is now owned by the WS tick feed (200ms cadence).
    The fast watcher no longer writes ltp:{underlying} — that would be a
    stale-clobber risk against the sub-second WS write."""
    r = _redis({UNDERLYING: OPEN_POSITION})
    with (
        patch.object(_jobs, "_is_market_open", return_value=True),
        patch.object(_jobs, "get_quote", return_value=GOOD_QUOTE),
        patch.object(_jobs, "get_option_quote_with_greeks", return_value=GOOD_GREEKS),
    ):
        await _fast_position_watcher(r)
    keys = {c.args[0] for c in r.setex.call_args_list}
    assert f"ltp:{UNDERLYING}" not in keys


@pytest.mark.asyncio
async def test_refreshes_option_and_greeks_with_30s_ttl():
    r = _redis({UNDERLYING: OPEN_POSITION})
    with (
        patch.object(_jobs, "_is_market_open", return_value=True),
        patch.object(_jobs, "get_quote", return_value=GOOD_QUOTE),
        patch.object(_jobs, "get_option_quote_with_greeks", return_value=GOOD_GREEKS),
    ):
        await _fast_position_watcher(r)
    keys = {c.args[0] for c in r.setex.call_args_list}
    assert f"market:{OPTION_SYM}" in keys
    assert f"greeks:{OPTION_SYM}" in keys
    for c in r.setex.call_args_list:
        k, ttl, _ = c.args
        if k in (f"market:{OPTION_SYM}", f"greeks:{OPTION_SYM}"):
            assert ttl == 30


@pytest.mark.asyncio
async def test_skips_option_refresh_when_greeks_returns_none():
    r = _redis({UNDERLYING: OPEN_POSITION})
    with (
        patch.object(_jobs, "_is_market_open", return_value=True),
        patch.object(_jobs, "get_quote", return_value=GOOD_QUOTE),
        patch.object(_jobs, "get_option_quote_with_greeks", return_value=None),
    ):
        await _fast_position_watcher(r)
    keys = {c.args[0] for c in r.setex.call_args_list}
    assert f"ltp:{UNDERLYING}" not in keys      # underlying never written by fast watcher
    assert f"market:{OPTION_SYM}" not in keys   # option skipped (no greeks)
    assert f"greeks:{OPTION_SYM}" not in keys


@pytest.mark.asyncio
async def test_position_without_option_skips_greeks():
    r = _redis({UNDERLYING: {**OPEN_POSITION, "option_symbol": None}})
    mock_gq = MagicMock()
    with (
        patch.object(_jobs, "_is_market_open", return_value=True),
        patch.object(_jobs, "get_quote", return_value=GOOD_QUOTE),
        patch.object(_jobs, "get_option_quote_with_greeks", mock_gq),
    ):
        await _fast_position_watcher(r)
    mock_gq.assert_not_called()


@pytest.mark.asyncio
async def test_error_in_one_position_does_not_abort_loop():
    """One position's Greeks fetch raising must not stop us from processing
    the next position. The fast watcher only calls get_option_quote_with_greeks
    per position now (underlying LTP comes from the WS tick feed)."""
    OPTION_A = "NSE:NIFTY26MAY24300CE"
    OPTION_B = "NSE:BANKNIFTY26MAY53300PE"
    r = _redis({
        UNDERLYING: {**OPEN_POSITION, "option_symbol": OPTION_A},
        "NSE:NIFTYBANK-INDEX": {"symbol": "NSE:NIFTYBANK-INDEX",
                                "side": "SELL", "option_symbol": OPTION_B},
    })
    seen = []

    def _greeks_side(opt_sym):
        seen.append(opt_sym)
        if opt_sym == OPTION_A:
            raise RuntimeError("simulated Fyers greeks error")
        return {"symbol": opt_sym, "ltp": 250.0, "delta": 0.5, "iv": 18.0}

    with (
        patch.object(_jobs, "_is_market_open", return_value=True),
        patch.object(_jobs, "get_option_quote_with_greeks", side_effect=_greeks_side),
    ):
        await _fast_position_watcher(r)

    # Both options had get_option_quote_with_greeks attempted — the error on A
    # did not prevent B from being processed.
    assert OPTION_A in seen
    assert OPTION_B in seen
    # B's writes landed despite A's failure.
    keys = {c.args[0] for c in r.setex.call_args_list}
    assert f"market:{OPTION_B}" in keys
    assert f"greeks:{OPTION_B}" in keys
