"""
Unit tests for the consolidation gate in simulation-engine/main.py.

The gate blocks BUY/SELL entries when price is inside the consolidation range
(is_consolidating=True AND range_breakout="NONE"). Breakout signals and
non-consolidating markets pass through unchanged.

All external dependencies are stubbed — no Redis, DB, or Fyers connections.
"""

import asyncio
import json
import sys
from datetime import datetime
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytz
import pytest

IST = pytz.timezone("Asia/Kolkata")

# ── Module stubs (must precede main.py import) ────────────────────────────────

def _stub(name, **attrs):
    m = ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


# redis
_aioredis = _stub("redis.asyncio", Redis=MagicMock())
_stub("redis", asyncio=_aioredis)

# fastapi
_fastapi = _stub("fastapi", FastAPI=MagicMock(), HTTPException=type("HTTPException", (Exception,), {}))

# pydantic — use real pydantic if available, otherwise stub BaseModel
try:
    from pydantic import BaseModel as _BaseModel  # noqa: F401
except ImportError:
    _stub("pydantic", BaseModel=object)

# analytics.pnl
_stub("analytics.pnl",
      compute_pnl_summary=AsyncMock(return_value={}),
      get_all_trades=AsyncMock(return_value=[]),
      get_open_positions=AsyncMock(return_value=[]))
_stub("analytics")

# models.schemas — Position must be a real class with model_dump_json()
class _FakePosition:
    def __init__(self, **kw): self.__dict__.update(kw)
    def model_dump_json(self): return json.dumps(self.__dict__)
    side = "BUY"

_stub("models.schemas", Position=_FakePosition, Trade=_FakePosition)
_stub("models")

# data_client
_stub("data_client",
      persist_trade=AsyncMock(),
      mark_decision_acted=AsyncMock())

# notifications
_stub("notifications.slack",
      notify_trade_opened=MagicMock(),
      notify_trade_closed=MagicMock())
_stub("notifications")

# portfolio.budget
_stub("portfolio.budget",
      allocate=AsyncMock(return_value=True),
      get_max_position_value=AsyncMock(return_value=50_000.0),
      release=AsyncMock(),
      initialize_budget=AsyncMock(),
      load_budget=AsyncMock(),
      reconcile_invested=AsyncMock(),
      compute_pnl_summary=AsyncMock(return_value={}))
_stub("portfolio")

# open_position mock — reset per test
_mock_open = AsyncMock(return_value=MagicMock())
_mock_close = AsyncMock(return_value=None)

_stub("execution.mock_broker",
      open_position=_mock_open,
      close_position=_mock_close)
_stub("execution.live_broker",
      open_position=AsyncMock(return_value=None),
      close_position=AsyncMock(return_value=None))
_stub("execution.invalidation_exit",
      check_invalidation_exit=MagicMock(return_value=None))
_stub("execution.exit_rules",
      check_exit=MagicMock(return_value=(False, "", 0.0, 0)),
      PREMIUM_SL_PCT=0.10,
      FIRST_MILESTONE_PCT=0.20,
      RANGING_MILESTONE_PCT=0.10)
_stub("execution")

# fyers
_stub("fyers.auth", get_fyers_client=MagicMock())
_stub("fyers.market_data", get_quote=MagicMock(return_value=None))
_stub("fyers.options",
      get_affordable_option=MagicMock(return_value=None),
      get_atm_option=MagicMock(return_value=None))
_stub("fyers.greeks", get_option_quote_with_greeks=MagicMock(return_value=None))
_stub("fyers")

# apscheduler
_stub("apscheduler.schedulers.asyncio", AsyncIOScheduler=MagicMock())
_stub("apscheduler.schedulers")
_stub("apscheduler")

import main as sim_main  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

SYMBOL = "NSE:NIFTY50-INDEX"
# Fixed time well within session (10:00 IST) — after ORB, before session close
_FAKE_NOW = IST.localize(datetime(2026, 5, 7, 10, 0, 0))


def _make_indicators(range_breakout="NONE", consolidation_pct=0.30, day_type="NARROW"):
    return json.dumps({
        "range_breakout":            range_breakout,
        "consolidation_pct":         consolidation_pct,
        "day_type":                  day_type,
        "nearest_resistance":        24500.0,
        "nearest_resistance_label":  "R1",
        "nearest_support":           24000.0,
        "nearest_support_label":     "S1",
        "cpr_width_pct":             0.10,
    })


def _make_decision(
    decision="BUY",
    confidence=0.80,
    range_breakout="NONE",
    consolidation_pct=0.30,
    day_type="NARROW",
):
    return {
        "symbol":          SYMBOL,
        "decision":        decision,
        "decision_id":     "test-id",
        "reasoning":       "test",
        "confidence":      str(confidence),
        "stop_loss":       "24000",
        "target":          "24500",
        "option_symbol":   "NSE:NIFTY2651224350CE",
        "option_strike":   "24350",
        "option_price":    "150.0",
        "option_lot_size": "50",
        "option_type":     "CE",
        "option_expiry":   "2026-05-12",
        "dte":             "5",
        "indicators":      _make_indicators(range_breakout, consolidation_pct, day_type),
    }


def _redis(ltp: float = 24200.0) -> AsyncMock:
    """Mock Redis that returns a market snapshot and simulation mode."""
    r = AsyncMock()
    market_payload = json.dumps({"ltp": ltp, "indicators": {}}).encode()

    async def _get(key):
        k = str(key)
        if f"market:{SYMBOL}" in k:
            return market_payload
        if "trading:mode" in k:
            return b"simulation"
        return None

    r.get      = AsyncMock(side_effect=_get)
    r.hget     = AsyncMock(return_value=None)
    r.exists   = AsyncMock(return_value=0)
    r.hset     = AsyncMock()
    r.zadd     = AsyncMock()
    r.set      = AsyncMock()
    r.setex    = AsyncMock()
    r.expire   = AsyncMock()
    r.hdel     = AsyncMock()
    r.hgetall  = AsyncMock(return_value={})
    return r


async def _run(data: dict, ltp: float = 24200.0) -> AsyncMock:
    """Drive _handle_decision and return the open_position mock for assertions."""
    sim_main.redis_client = _redis(ltp)
    _mock_open.reset_mock()
    with patch("main.datetime") as mock_dt:
        mock_dt.now.return_value = _FAKE_NOW
        # Keep datetime() constructor working for any other uses
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        await sim_main._handle_decision(data)
    return _mock_open


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestConsolidationGateBlocks:
    """Price inside consolidation range → no trade opened."""

    def test_buy_blocked_inside_range(self):
        data = _make_decision(decision="BUY", range_breakout="NONE", consolidation_pct=0.30)
        result = asyncio.get_event_loop().run_until_complete(_run(data))
        result.assert_not_called()

    def test_sell_blocked_inside_range(self):
        data = _make_decision(decision="SELL", range_breakout="NONE", consolidation_pct=0.30)
        result = asyncio.get_event_loop().run_until_complete(_run(data))
        result.assert_not_called()

    def test_blocked_at_upper_boundary(self):
        """consolidation_pct=0.39 is still consolidating (< 0.40 threshold)."""
        data = _make_decision(decision="BUY", range_breakout="NONE", consolidation_pct=0.39)
        result = asyncio.get_event_loop().run_until_complete(_run(data))
        result.assert_not_called()

    def test_tight_range_blocked(self):
        """Very tight consolidation (10% of ATR) is clearly blocked."""
        data = _make_decision(decision="BUY", range_breakout="NONE", consolidation_pct=0.10)
        result = asyncio.get_event_loop().run_until_complete(_run(data))
        result.assert_not_called()


class TestConsolidationGatePasses:
    """Breakouts and wide markets pass through the gate."""

    def test_buy_allowed_on_breakout_high(self):
        data = _make_decision(decision="BUY", range_breakout="BREAKOUT_HIGH", consolidation_pct=0.30)
        result = asyncio.get_event_loop().run_until_complete(_run(data))
        result.assert_called_once()

    def test_sell_allowed_on_breakout_low(self):
        data = _make_decision(decision="SELL", range_breakout="BREAKOUT_LOW", consolidation_pct=0.30)
        result = asyncio.get_event_loop().run_until_complete(_run(data))
        result.assert_called_once()

    def test_not_consolidating_passes(self):
        """consolidation_pct >= 0.40 — wide trending market, gate does not fire."""
        data = _make_decision(decision="BUY", range_breakout="NONE", consolidation_pct=0.50)
        result = asyncio.get_event_loop().run_until_complete(_run(data))
        result.assert_called_once()

    def test_exactly_at_threshold_passes(self):
        """consolidation_pct=0.40 is not consolidating (< 0.40 is the condition)."""
        data = _make_decision(decision="BUY", range_breakout="NONE", consolidation_pct=0.40)
        result = asyncio.get_event_loop().run_until_complete(_run(data))
        result.assert_called_once()

    def test_missing_range_breakout_passes(self):
        """Absent range_breakout defaults to '' which != 'NONE' — gate does not fire."""
        data = _make_decision(decision="BUY", consolidation_pct=0.30)
        ind = json.loads(data["indicators"])
        del ind["range_breakout"]
        data["indicators"] = json.dumps(ind)
        result = asyncio.get_event_loop().run_until_complete(_run(data))
        result.assert_called_once()

    def test_wide_market_breakout_high_passes(self):
        """Wide market (consolidation_pct=0.60) with BREAKOUT_HIGH still passes."""
        data = _make_decision(decision="BUY", range_breakout="BREAKOUT_HIGH", consolidation_pct=0.60)
        result = asyncio.get_event_loop().run_until_complete(_run(data))
        result.assert_called_once()

    def test_hold_unaffected(self):
        """HOLD decisions skip all entry gates — open_position is never called."""
        data = _make_decision(decision="HOLD", range_breakout="NONE", consolidation_pct=0.30)
        result = asyncio.get_event_loop().run_until_complete(_run(data))
        result.assert_not_called()
