"""
Unit tests for the ORB breakout gate in simulation-engine/main.py.

Gate logic:
  BUY  is blocked when price <= orb_high * 1.002  (no confirmed breakout above range)
  SELL is blocked when price >= orb_low  * 0.998  (no confirmed breakdown below range)

The 0.20% buffer is data-derived from 141 days of NIFTY/BANKNIFTY history.
When orb_high=0 or orb_low=0 the gate is silently skipped (ORB not yet formed).

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
_stub("fastapi", FastAPI=MagicMock(), HTTPException=type("HTTPException", (Exception,), {}))

# pydantic
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

# models.schemas
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

# execution stubs — open_position replaced per-test via patch.object
_stub("execution.mock_broker",
      open_position=AsyncMock(return_value=MagicMock()),
      close_position=AsyncMock(return_value=None))
_stub("execution.live_broker",
      open_position=AsyncMock(return_value=None),
      close_position=AsyncMock(return_value=None))
_stub("execution.invalidation_exit",
      check_invalidation_exit=MagicMock(return_value=None),
      build_invalidation_levels=MagicMock(return_value=None))
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
# 10:00 IST — after ORB close (09:30), well within session
_FAKE_NOW = IST.localize(datetime(2026, 5, 12, 10, 0, 0))

# ORB levels used throughout: high=24500, low=24200
# BUY breakout threshold  = 24500 * 1.002 = 24549.0
# SELL breakdown threshold = 24200 * 0.998 = 24151.6
_ORB_HIGH = 24500.0
_ORB_LOW  = 24200.0


def _make_indicators(orb_high=_ORB_HIGH, orb_low=_ORB_LOW,
                     consolidation_pct=0.50, range_breakout="NONE",
                     day_type="TRENDING"):
    return json.dumps({
        "orb_high":                  orb_high,
        "orb_low":                   orb_low,
        "range_breakout":            range_breakout,
        "consolidation_pct":         consolidation_pct,
        "day_type":                  day_type,
        "nearest_resistance":        25000.0,
        "nearest_resistance_label":  "R3",
        "nearest_support":           23000.0,
        "nearest_support_label":     "S3",
        "cpr_width_pct":             0.10,
    })


def _make_decision(decision="BUY", confidence=0.80, **ind_kwargs):
    return {
        "symbol":          SYMBOL,
        "decision":        decision,
        "decision_id":     "test-orb-id",
        "reasoning":       "test",
        "confidence":      str(confidence),
        "stop_loss":       "24000",
        "target":          "25000",
        "option_symbol":   "NSE:NIFTY2651224500CE",
        "option_strike":   "24500",
        "option_price":    "150.0",
        "option_lot_size": "50",
        "option_type":     "CE",
        "option_expiry":   "2026-05-26",
        "dte":             "14",
        "indicators":      _make_indicators(**ind_kwargs),
    }


def _redis(ltp: float = 24200.0) -> AsyncMock:
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


async def _run(data: dict, ltp: float = 24200.0):
    """
    Drive _handle_decision with a fresh open_position mock.
    Uses patch.object so the mock is always the one main.py calls,
    regardless of which test file was imported first.
    """
    sim_main.redis_client = _redis(ltp)
    mock_open = AsyncMock(return_value=MagicMock())
    with patch.object(sim_main.mock_broker, "open_position", mock_open), \
         patch("main.datetime") as mock_dt:
        mock_dt.now.return_value = _FAKE_NOW
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        await sim_main._handle_decision(data)
    return mock_open


# ── Tests: gate blocks ────────────────────────────────────────────────────────

class TestOrbGateBlocks:
    """Entries inside or at the ORB boundary are blocked."""

    def test_buy_blocked_when_price_inside_orb(self):
        """Price is inside the opening range — no valid breakout."""
        data = _make_decision(decision="BUY")
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=24350.0))
        result.assert_not_called()

    def test_buy_blocked_at_exact_orb_high(self):
        """Price exactly equals orb_high — still inside (≤ threshold)."""
        data = _make_decision(decision="BUY")
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=_ORB_HIGH))
        result.assert_not_called()

    def test_buy_blocked_at_exact_breakout_threshold(self):
        """Price == orb_high × 1.002 — threshold is inclusive (≤), still blocked."""
        threshold = round(_ORB_HIGH * 1.002, 2)
        data = _make_decision(decision="BUY")
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=threshold))
        result.assert_not_called()

    def test_sell_blocked_when_price_inside_orb(self):
        """Price is inside the opening range — no valid breakdown."""
        data = _make_decision(decision="SELL")
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=24350.0))
        result.assert_not_called()

    def test_sell_blocked_at_exact_orb_low(self):
        """Price exactly equals orb_low — still inside (≥ threshold)."""
        data = _make_decision(decision="SELL")
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=_ORB_LOW))
        result.assert_not_called()

    def test_sell_blocked_at_exact_breakdown_threshold(self):
        """Price == orb_low × 0.998 — threshold is inclusive (≥), still blocked."""
        threshold = round(_ORB_LOW * 0.998, 2)
        data = _make_decision(decision="SELL")
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=threshold))
        result.assert_not_called()


# ── Tests: gate passes ────────────────────────────────────────────────────────

class TestOrbGatePasses:
    """Valid breakouts above/below the ORB pass through the gate."""

    def test_buy_passes_on_clear_breakout_above_orb(self):
        """Price is clearly above orb_high × 1.002 — confirmed upside breakout."""
        data = _make_decision(decision="BUY")
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=24600.0))
        result.assert_called_once()

    def test_buy_passes_just_above_breakout_threshold(self):
        """Price is 1 tick above orb_high × 1.002 — just clears the gate."""
        threshold = _ORB_HIGH * 1.002
        data = _make_decision(decision="BUY")
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=threshold + 0.05))
        result.assert_called_once()

    def test_sell_passes_on_clear_breakdown_below_orb(self):
        """Price is clearly below orb_low × 0.998 — confirmed downside breakdown."""
        data = _make_decision(decision="SELL")
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=24100.0))
        result.assert_called_once()

    def test_sell_passes_just_below_breakdown_threshold(self):
        """Price is 1 tick below orb_low × 0.998 — just clears the gate."""
        threshold = _ORB_LOW * 0.998
        data = _make_decision(decision="SELL")
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=threshold - 0.05))
        result.assert_called_once()


# ── Tests: gate skipped when ORB not yet formed ───────────────────────────────

class TestOrbGateSkipped:
    """When orb_high=0 or orb_low=0, the gate is not applied."""

    def test_buy_not_blocked_when_orb_not_yet_formed(self):
        """Before 09:30 compute cycle, ORB is zero — gate must not block."""
        data = _make_decision(decision="BUY", orb_high=0.0, orb_low=0.0)
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=24200.0))
        result.assert_called_once()

    def test_sell_not_blocked_when_orb_not_yet_formed(self):
        data = _make_decision(decision="SELL", orb_high=0.0, orb_low=0.0)
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=24200.0))
        result.assert_called_once()

    def test_missing_orb_fields_treated_as_zero(self):
        """indicators dict with no orb_high/orb_low keys — gate skips gracefully."""
        data = _make_decision(decision="BUY")
        ind = json.loads(data["indicators"])
        del ind["orb_high"]
        del ind["orb_low"]
        data["indicators"] = json.dumps(ind)
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=24200.0))
        result.assert_called_once()

    def test_hold_unaffected_by_gate(self):
        """HOLD decisions never reach any entry gate."""
        data = _make_decision(decision="HOLD")
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=24200.0))
        result.assert_not_called()


# ── Tests: ORB-after-break relaxation ─────────────────────────────────────────


def _clear_orb_cache():
    """Reset the per-session 'ORB broken' cache between tests."""
    sim_main._orb_broken_today.clear()


def _historical_candles_response(session_high: float, session_low: float):
    """Build a fake /historical-data response with a high/low pair embedded."""
    candles = [
        {"time": "2026-05-12T04:00:00+00:00", "open": 24300, "high": session_high,
         "low": 24300, "close": 24300, "volume": 1000},
        {"time": "2026-05-12T04:01:00+00:00", "open": 24300, "high": 24300,
         "low": session_low, "close": 24300, "volume": 1000},
    ]
    return MagicMock(status_code=200, json=lambda: {"candles": candles})


class TestOrbAfterBreakRelaxation:
    """
    Once ORB is broken (either direction) at any point today, the gate is
    disabled for the rest of the session. Backtest of 147 days shows ~75% of
    break-days have material follow-through in some direction; only ~10-14%
    are true false-breakout mean reversions.
    """

    def setup_method(self):
        _clear_orb_cache()

    # ── fast path: live price itself outside threshold ──

    def test_buy_passes_when_live_price_above_threshold(self):
        """Live price > orb_high × 1.002 — break confirmed by live LTP itself."""
        data = _make_decision(decision="BUY")
        # 24600 > 24549.0 (threshold). Fast path sets the cache.
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=24600.0))
        result.assert_called_once()
        # cache populated by the fast path
        assert sim_main._orb_broken_today  # non-empty

    def test_buy_passes_inside_orb_after_break_was_cached(self):
        """
        After a prior break (cached), a new BUY signal at a price *inside* ORB
        is still allowed — the gate is disabled for the rest of the day.
        """
        # Manually populate cache as if break already happened earlier
        today = _FAKE_NOW.date()
        sim_main._orb_broken_today[(SYMBOL, today)] = True

        data = _make_decision(decision="BUY")
        # ltp 24350 is inside ORB — would normally be blocked
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=24350.0))
        result.assert_called_once()

    def test_sell_passes_inside_orb_after_break_was_cached(self):
        """Same idea for the SELL side."""
        today = _FAKE_NOW.date()
        sim_main._orb_broken_today[(SYMBOL, today)] = True

        data = _make_decision(decision="SELL")
        result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=24350.0))
        result.assert_called_once()

    # ── slow path: data-service reports the break already happened ──

    def test_buy_passes_when_data_service_reports_prior_high_break(self):
        """
        Live price is inside ORB now, but data-service reports today's
        session high already crossed the upper threshold earlier → gate is
        disabled and BUY passes.
        """
        # Session high 24600 > th_high (24549), session_low inside ORB
        fake_resp = _historical_candles_response(session_high=24600.0, session_low=24300.0)
        ac_instance = AsyncMock()
        ac_instance.get = AsyncMock(return_value=fake_resp)
        ac_ctx = AsyncMock()
        ac_ctx.__aenter__.return_value = ac_instance

        data = _make_decision(decision="BUY")
        with patch("main.httpx.AsyncClient", return_value=ac_ctx):
            result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=24350.0))
        result.assert_called_once()

    def test_sell_passes_when_data_service_reports_prior_low_break(self):
        """Symmetric to above: prior session low broke down → gate disabled."""
        fake_resp = _historical_candles_response(session_high=24400.0, session_low=24100.0)
        ac_instance = AsyncMock()
        ac_instance.get = AsyncMock(return_value=fake_resp)
        ac_ctx = AsyncMock()
        ac_ctx.__aenter__.return_value = ac_instance

        data = _make_decision(decision="SELL")
        with patch("main.httpx.AsyncClient", return_value=ac_ctx):
            result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=24350.0))
        result.assert_called_once()

    # ── gate still blocks when no break has happened today ──

    def test_buy_still_blocked_when_no_break_today(self):
        """Data-service reports no break; live price is inside ORB → gate blocks."""
        fake_resp = _historical_candles_response(session_high=24500.0, session_low=24200.0)
        ac_instance = AsyncMock()
        ac_instance.get = AsyncMock(return_value=fake_resp)
        ac_ctx = AsyncMock()
        ac_ctx.__aenter__.return_value = ac_instance

        data = _make_decision(decision="BUY")
        with patch("main.httpx.AsyncClient", return_value=ac_ctx):
            result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=24350.0))
        result.assert_not_called()

    def test_data_service_failure_fails_open_to_gate(self):
        """
        If data-service errors / times out, the helper fails open (returns False)
        and the original gate logic applies. A BUY at a price inside ORB is
        therefore blocked as normal.
        """
        ac_instance = AsyncMock()
        ac_instance.get = AsyncMock(side_effect=Exception("boom"))
        ac_ctx = AsyncMock()
        ac_ctx.__aenter__.return_value = ac_instance

        data = _make_decision(decision="BUY")
        with patch("main.httpx.AsyncClient", return_value=ac_ctx):
            result = asyncio.get_event_loop().run_until_complete(_run(data, ltp=24350.0))
        result.assert_not_called()
