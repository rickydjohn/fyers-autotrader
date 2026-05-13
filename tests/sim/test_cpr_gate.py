"""
Unit tests for the CPR gate in simulation-engine/main.py.

Gate logic:
  upper = max(CPR_TC, CPR_BC)   — handles both normal and inverted CPR
  lower = min(CPR_TC, CPR_BC)

  BUY  blocked when price <= upper * 1.002  (not confirmed above band)
  SELL blocked when price >= lower * 0.998  (not confirmed below band)

  Gate skipped when cpr_tc=0 or cpr_bc=0.

Normal CPR  : TC > BC  (e.g. TC=24300, BC=24200) → upper=TC, lower=BC
Inverted CPR: BC > TC  (e.g. BC=23553, TC=23437) → upper=BC, lower=TC
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

# ── Module stubs ──────────────────────────────────────────────────────────────

def _stub(name, **attrs):
    m = ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


_aioredis = _stub("redis.asyncio", Redis=MagicMock())
_stub("redis", asyncio=_aioredis)
_stub("fastapi", FastAPI=MagicMock(), HTTPException=type("HTTPException", (Exception,), {}))

try:
    from pydantic import BaseModel as _BM  # noqa: F401
except ImportError:
    _stub("pydantic", BaseModel=object)

_stub("analytics.pnl",
      compute_pnl_summary=AsyncMock(return_value={}),
      get_all_trades=AsyncMock(return_value=[]),
      get_open_positions=AsyncMock(return_value=[]))
_stub("analytics")

class _FakePos:
    def __init__(self, **kw): self.__dict__.update(kw)
    def model_dump_json(self): return json.dumps(self.__dict__)
    side = "BUY"

_stub("models.schemas", Position=_FakePos, Trade=_FakePos)
_stub("models")
_stub("data_client", persist_trade=AsyncMock(), mark_decision_acted=AsyncMock())
_stub("notifications.slack",
      notify_trade_opened=MagicMock(), notify_trade_closed=MagicMock())
_stub("notifications")
_stub("portfolio.budget",
      allocate=AsyncMock(return_value=True),
      get_max_position_value=AsyncMock(return_value=50_000.0),
      release=AsyncMock(), initialize_budget=AsyncMock(),
      load_budget=AsyncMock(), reconcile_invested=AsyncMock(),
      compute_pnl_summary=AsyncMock(return_value={}))
_stub("portfolio")
_stub("execution.mock_broker",
      open_position=AsyncMock(return_value=MagicMock()),
      close_position=AsyncMock(return_value=None))
_stub("execution.live_broker",
      open_position=AsyncMock(return_value=None),
      close_position=AsyncMock(return_value=None))
_stub("execution.exit_rules",
      check_exit=MagicMock(return_value=(False, "", 0.0, 0)),
      PREMIUM_SL_PCT=0.10,
      FIRST_MILESTONE_PCT=0.20,
      RANGING_MILESTONE_PCT=0.10)
_stub("execution")
_stub("fyers.auth", get_fyers_client=MagicMock())
_stub("fyers.market_data", get_quote=MagicMock(return_value=None))
_stub("fyers.options",
      get_affordable_option=MagicMock(return_value=None),
      get_atm_option=MagicMock(return_value=None))
_stub("fyers.greeks", get_option_quote_with_greeks=MagicMock(return_value=None))
_stub("fyers")
_stub("apscheduler.schedulers.asyncio", AsyncIOScheduler=MagicMock())
_stub("apscheduler.schedulers")
_stub("apscheduler")

import main as sim_main  # noqa: E402

# ── Helpers ───────────────────────────────────────────────────────────────────

SYMBOL   = "NSE:NIFTY50-INDEX"
_NOW     = IST.localize(datetime(2026, 5, 13, 10, 0, 0))

# Normal CPR  (TC=24300 > BC=24200): upper=24300, lower=24200
_TC_NORM = 24300.0
_BC_NORM = 24200.0

# Inverted CPR (BC=23553 > TC=23437): upper=BC=23553, lower=TC=23437
_TC_INV  = 23437.0
_BC_INV  = 23553.0


def _make_decision(decision="BUY", cpr_tc=_TC_NORM, cpr_bc=_BC_NORM, **kw):
    ind = {
        "orb_high": 0.0, "orb_low": 0.0,
        "range_breakout": "NONE", "consolidation_pct": 0.80,
        "day_type": "TRENDING",
        "nearest_resistance": 25000.0, "nearest_resistance_label": "R3",
        "nearest_support":    23000.0, "nearest_support_label":    "S3",
        "cpr_width_pct": 0.42,
        "cpr_tc": cpr_tc,
        "cpr_bc": cpr_bc,
    }
    ind.update(kw)
    return {
        "symbol":          SYMBOL,
        "decision":        decision,
        "decision_id":     "test-cpr-id",
        "reasoning":       "test",
        "confidence":      "0.80",
        "stop_loss":       "23000",
        "target":          "25000",
        "option_symbol":   "NSE:NIFTY2651924300CE",
        "option_strike":   "24300",
        "option_price":    "150.0",
        "option_lot_size": "50",
        "option_type":     "CE",
        "option_expiry":   "2026-05-26",
        "dte":             "13",
        "indicators":      json.dumps(ind),
    }


def _redis(ltp: float) -> AsyncMock:
    r = AsyncMock()
    payload = json.dumps({"ltp": ltp, "indicators": {}}).encode()

    async def _get(key):
        k = str(key)
        if f"market:{SYMBOL}" in k:
            return payload
        if "trading:mode" in k:
            return b"simulation"
        return None

    r.get     = AsyncMock(side_effect=_get)
    r.hget    = AsyncMock(return_value=None)
    r.exists  = AsyncMock(return_value=0)
    r.hset    = AsyncMock()
    r.zadd    = AsyncMock()
    r.set     = AsyncMock()
    r.setex   = AsyncMock()
    r.expire  = AsyncMock()
    r.hdel    = AsyncMock()
    r.hgetall = AsyncMock(return_value={})
    return r


async def _run(data: dict, ltp: float):
    sim_main.redis_client = _redis(ltp)
    mock_open = AsyncMock(return_value=MagicMock())
    with patch.object(sim_main.mock_broker, "open_position", mock_open), \
         patch("main.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        await sim_main._handle_decision(data)
    return mock_open


# ── Normal CPR (TC=24300, BC=24200) ──────────────────────────────────────────
# upper=24300, BUY threshold = 24300 × 1.002 = 24348.6
# lower=24200, SELL threshold = 24200 × 0.998 = 24151.6

class TestCprGateNormal:
    """Normal CPR where TC > BC."""

    def test_buy_blocked_inside_cpr_band(self):
        data = _make_decision("BUY", cpr_tc=_TC_NORM, cpr_bc=_BC_NORM)
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=24250.0))
        result.assert_not_called()

    def test_buy_blocked_at_exact_upper_boundary(self):
        data = _make_decision("BUY", cpr_tc=_TC_NORM, cpr_bc=_BC_NORM)
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=_TC_NORM))
        result.assert_not_called()

    def test_buy_blocked_at_exact_buffer_threshold(self):
        threshold = round(_TC_NORM * 1.002, 2)
        data = _make_decision("BUY", cpr_tc=_TC_NORM, cpr_bc=_BC_NORM)
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=threshold))
        result.assert_not_called()

    def test_buy_passes_just_above_buffer_threshold(self):
        threshold = _TC_NORM * 1.002
        data = _make_decision("BUY", cpr_tc=_TC_NORM, cpr_bc=_BC_NORM)
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=threshold + 0.05))
        result.assert_called_once()

    def test_buy_passes_clearly_above_cpr(self):
        data = _make_decision("BUY", cpr_tc=_TC_NORM, cpr_bc=_BC_NORM)
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=24500.0))
        result.assert_called_once()

    def test_sell_blocked_inside_cpr_band(self):
        data = _make_decision("SELL", cpr_tc=_TC_NORM, cpr_bc=_BC_NORM)
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=24250.0))
        result.assert_not_called()

    def test_sell_blocked_at_exact_lower_boundary(self):
        data = _make_decision("SELL", cpr_tc=_TC_NORM, cpr_bc=_BC_NORM)
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=_BC_NORM))
        result.assert_not_called()

    def test_sell_blocked_at_exact_buffer_threshold(self):
        threshold = round(_BC_NORM * 0.998, 2)
        data = _make_decision("SELL", cpr_tc=_TC_NORM, cpr_bc=_BC_NORM)
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=threshold))
        result.assert_not_called()

    def test_sell_passes_just_below_buffer_threshold(self):
        threshold = _BC_NORM * 0.998
        data = _make_decision("SELL", cpr_tc=_TC_NORM, cpr_bc=_BC_NORM)
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=threshold - 0.05))
        result.assert_called_once()

    def test_sell_passes_clearly_below_cpr(self):
        data = _make_decision("SELL", cpr_tc=_TC_NORM, cpr_bc=_BC_NORM)
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=23900.0))
        result.assert_called_once()


# ── Inverted CPR (BC=23553 > TC=23437) — today's scenario ────────────────────
# upper=BC=23553, BUY threshold = 23553 × 1.002 = 23600.1
# lower=TC=23437, SELL threshold = 23437 × 0.998 = 23390.1

class TestCprGateInverted:
    """Inverted CPR where BC > TC — upper boundary is BC, not TC."""

    def test_buy_blocked_when_only_above_tc_not_bc(self):
        """Today's scenario: price above TC (lower) but not above BC (upper)."""
        # price=23500 > TC=23437 but < BC=23553 → still inside band
        data = _make_decision("BUY", cpr_tc=_TC_INV, cpr_bc=_BC_INV)
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=23500.0))
        result.assert_not_called()

    def test_buy_blocked_just_above_bc_within_buffer(self):
        """Price is 0.07% above BC — the exact today scenario, must be blocked."""
        price = round(_BC_INV * 1.0007, 2)   # ~23569
        data  = _make_decision("BUY", cpr_tc=_TC_INV, cpr_bc=_BC_INV)
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=price))
        result.assert_not_called()

    def test_buy_blocked_at_bc_buffer_threshold(self):
        # Use the exact float threshold — rounding up would exceed it and pass
        threshold = _BC_INV * 1.002
        data = _make_decision("BUY", cpr_tc=_TC_INV, cpr_bc=_BC_INV)
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=threshold))
        result.assert_not_called()

    def test_buy_passes_above_bc_buffer(self):
        threshold = _BC_INV * 1.002
        data = _make_decision("BUY", cpr_tc=_TC_INV, cpr_bc=_BC_INV)
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=threshold + 0.05))
        result.assert_called_once()

    def test_sell_blocked_just_below_tc_within_buffer(self):
        """SELL: lower boundary is TC=23437; price just barely below TC."""
        price = round(_TC_INV * 0.9993, 2)
        data  = _make_decision("SELL", cpr_tc=_TC_INV, cpr_bc=_BC_INV)
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=price))
        result.assert_not_called()

    def test_sell_passes_clearly_below_tc(self):
        threshold = _TC_INV * 0.998
        data = _make_decision("SELL", cpr_tc=_TC_INV, cpr_bc=_BC_INV)
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=threshold - 0.05))
        result.assert_called_once()


# ── Gate skipped when CPR not available ──────────────────────────────────────

class TestCprGateSkipped:
    """Gate is silently skipped when TC or BC is zero."""

    def test_buy_not_blocked_when_cpr_zero(self):
        data = _make_decision("BUY", cpr_tc=0.0, cpr_bc=0.0)
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=24250.0))
        result.assert_called_once()

    def test_sell_not_blocked_when_cpr_zero(self):
        data = _make_decision("SELL", cpr_tc=0.0, cpr_bc=0.0)
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=24250.0))
        result.assert_called_once()

    def test_missing_cpr_fields_treated_as_zero(self):
        data = _make_decision("BUY")
        ind  = json.loads(data["indicators"])
        del ind["cpr_tc"]
        del ind["cpr_bc"]
        data["indicators"] = json.dumps(ind)
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=24250.0))
        result.assert_called_once()

    def test_hold_unaffected(self):
        data = _make_decision("HOLD")
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=24250.0))
        result.assert_not_called()
