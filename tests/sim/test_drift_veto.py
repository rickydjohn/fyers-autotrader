"""
Unit tests for the drift veto gate in simulation-engine/main.py.

Gate logic (configurable via settings.drift_veto_pct, default 0.10%):
  - At order time, fetch fresh live LTP via _fetch_live_ltp.
  - drift = (live_ltp - snapshot_price) / snapshot_price
  - BUY  blocked when drift < -threshold (price dropped against bullish signal)
  - SELL blocked when drift > +threshold (price rose against bearish signal)
  - Fail-open: missing snapshot_price OR _fetch_live_ltp returns None → no veto

Today's BANKNIFTY 53300PE trade context (2026-05-14): snapshot=53,341 →
live~53,389 at order time = +0.09% drift. Below the 0.10% default, so this
specific trade would still pass — the user can tune DRIFT_VETO_PCT down if
they want stricter staleness protection.
"""

import asyncio
import json
import sys
from datetime import datetime
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytz

IST = pytz.timezone("Asia/Kolkata")


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
_stub("execution.invalidation_exit",
      check_invalidation_exit=MagicMock(return_value=None))
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


SYMBOL = "NSE:NIFTYBANK-INDEX"
_NOW   = IST.localize(datetime(2026, 5, 13, 10, 0, 0))


def _make_decision(decision: str, snapshot_price: float, **kw):
    """Indicator dict mirrors what core-engine publishes; the 'price' field is what
    the LLM saw at decision time."""
    ind = {
        "price":              snapshot_price,
        "orb_high":           0.0,
        "orb_low":            0.0,
        "range_breakout":     "NONE",
        "consolidation_pct":  0.80,
        "day_type":           "TRENDING",
        "nearest_resistance": snapshot_price * 1.05,
        "nearest_resistance_label": "R3",
        "nearest_support":    snapshot_price * 0.95,
        "nearest_support_label":    "S3",
        "cpr_width_pct":      0.42,
        "cpr_tc":             0.0,
        "cpr_bc":             0.0,
    }
    ind.update(kw)
    return {
        "symbol":          SYMBOL,
        "decision":        decision,
        "decision_id":     "test-drift-id",
        "reasoning":       "test",
        "confidence":      "0.80",
        "stop_loss":       str(snapshot_price * 0.99),
        "target":          str(snapshot_price * 1.02),
        "option_symbol":   "NSE:BANKNIFTY26MAY53300PE",
        "option_strike":   "53300",
        "option_price":    "789.0",
        "option_lot_size": "30",
        "option_type":     "PE",
        "option_expiry":   "2026-05-26",
        "dte":             "13",
        "indicators":      json.dumps(ind),
    }


def _redis(market_ltp: float) -> AsyncMock:
    """Redis stub. The market:{symbol} key holds the stale scan-time price —
    drift veto's job is to override this with a fresher reading."""
    r = AsyncMock()
    payload = json.dumps({"ltp": market_ltp, "indicators": {}}).encode()

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


async def _run(data: dict, market_ltp: float, live_ltp):
    """Run _handle_decision with stubbed redis, broker, and _fetch_live_ltp."""
    sim_main.redis_client = _redis(market_ltp)
    mock_open = AsyncMock(return_value=MagicMock())
    fetch_mock = AsyncMock(return_value=live_ltp)
    with patch.object(sim_main.mock_broker, "open_position", mock_open), \
         patch.object(sim_main, "_fetch_live_ltp", fetch_mock), \
         patch("main.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        await sim_main._handle_decision(data)
    return mock_open, fetch_mock


# ── Adverse drift blocks the entry ─────────────────────────────────────────────

class TestDriftVetoBlocks:
    """Adverse drift beyond threshold → order skipped."""

    def test_buy_blocked_when_price_drops_more_than_threshold(self):
        """BUY signal, but price dropped 0.20% by order time → veto."""
        snapshot = 53_341.0
        live     = snapshot * (1 - 0.0020)  # -0.20% drift (well past 0.10%)
        data = _make_decision("BUY", snapshot_price=snapshot)
        opened, fetched = asyncio.get_event_loop().run_until_complete(
            _run(data, market_ltp=snapshot, live_ltp=live)
        )
        fetched.assert_awaited_once_with(SYMBOL)
        opened.assert_not_called()

    def test_sell_blocked_when_price_rises_more_than_threshold(self):
        """Today's case scaled up: SELL signal but price rallied 0.20% → veto."""
        snapshot = 53_341.0
        live     = snapshot * (1 + 0.0020)  # +0.20% drift
        data = _make_decision("SELL", snapshot_price=snapshot)
        opened, fetched = asyncio.get_event_loop().run_until_complete(
            _run(data, market_ltp=snapshot, live_ltp=live)
        )
        fetched.assert_awaited_once_with(SYMBOL)
        opened.assert_not_called()


# ── Favorable drift never blocks ───────────────────────────────────────────────

class TestDriftVetoFavorable:
    """Drift in the same direction as the signal is free profit — never blocks."""

    def test_buy_passes_when_price_already_rose(self):
        """Price moved UP after BUY snapshot — we missed some profit but signal still valid."""
        snapshot = 53_341.0
        live     = snapshot * (1 + 0.0030)  # +0.30% in BUY's favor
        data = _make_decision("BUY", snapshot_price=snapshot)
        opened, _ = asyncio.get_event_loop().run_until_complete(
            _run(data, market_ltp=snapshot, live_ltp=live)
        )
        opened.assert_called_once()

    def test_sell_passes_when_price_already_fell(self):
        """Price moved DOWN after SELL snapshot — favorable, proceed."""
        snapshot = 53_341.0
        live     = snapshot * (1 - 0.0030)
        data = _make_decision("SELL", snapshot_price=snapshot)
        opened, _ = asyncio.get_event_loop().run_until_complete(
            _run(data, market_ltp=snapshot, live_ltp=live)
        )
        opened.assert_called_once()


# ── Drift just below threshold passes ──────────────────────────────────────────

class TestDriftVetoBoundary:
    """Drift just inside the threshold passes; just outside blocks."""

    def test_buy_passes_just_inside_threshold(self):
        snapshot = 53_341.0
        # 0.09% drop — still inside 0.10% default tolerance
        live = snapshot * (1 - 0.0009)
        data = _make_decision("BUY", snapshot_price=snapshot)
        opened, _ = asyncio.get_event_loop().run_until_complete(
            _run(data, market_ltp=snapshot, live_ltp=live)
        )
        opened.assert_called_once()

    def test_sell_passes_just_inside_threshold(self):
        snapshot = 53_341.0
        live = snapshot * (1 + 0.0009)
        data = _make_decision("SELL", snapshot_price=snapshot)
        opened, _ = asyncio.get_event_loop().run_until_complete(
            _run(data, market_ltp=snapshot, live_ltp=live)
        )
        opened.assert_called_once()


# ── Fail-open: veto silent when data is missing ────────────────────────────────

class TestDriftVetoFailOpen:
    """Missing snapshot or unreachable Fyers quote → veto skipped, no new failure mode."""

    def test_no_snapshot_price_skips_veto(self):
        """ind_dict has no 'price' field → veto is silently skipped."""
        data = _make_decision("BUY", snapshot_price=53_341.0)
        ind = json.loads(data["indicators"])
        del ind["price"]
        data["indicators"] = json.dumps(ind)
        opened, fetched = asyncio.get_event_loop().run_until_complete(
            _run(data, market_ltp=53_341.0, live_ltp=53_200.0)
        )
        # Even with adverse "live" price, no veto because snapshot is missing
        fetched.assert_not_awaited()
        opened.assert_called_once()

    def test_live_quote_failure_skips_veto(self):
        """_fetch_live_ltp returns None (Fyers down, proxy hiccup) → veto skipped."""
        snapshot = 53_341.0
        data = _make_decision("BUY", snapshot_price=snapshot)
        opened, fetched = asyncio.get_event_loop().run_until_complete(
            _run(data, market_ltp=snapshot, live_ltp=None)
        )
        fetched.assert_awaited_once_with(SYMBOL)
        opened.assert_called_once()


# ── Fresh price flows into downstream gates (regression: 2026-05-14 CPR leak) ──

class TestFreshPriceFeedsGates:
    """The drift veto's live-LTP fetch runs BEFORE the other gates. When drift is
    within tolerance, the fresh LTP replaces the stale scan-time price so ORB /
    CPR / consolidation / entry-proximity all evaluate against the actual market
    price at order time.

    Regression test for 2026-05-14: BANKNIFTY snapshot price 53,341 was below
    the CPR-lower threshold (53,413), so the gate let the SELL through. But the
    actual market at order fire was 53,415 — above the threshold — and the
    trade should have been blocked. Fix: gate evaluates the fresh price."""

    def test_cpr_gate_blocks_with_fresh_price_even_when_snapshot_would_pass(self):
        """Scenario: snapshot below CPR-lower (would pass), fresh LTP above
        CPR-lower (must block). Drift kept below drift_veto_pct so the veto
        itself doesn't fire — proving the gate, not the veto, blocks."""
        # cpr_lower threshold for SELL = cpr_lower * 0.998
        # cpr_lower=53520 → threshold ≈ 53,413
        # snapshot=53,400 (below threshold) → snapshot-time gate would PASS
        # live=53,415   (above threshold) → fresh-time gate must BLOCK
        # drift = (53,415-53,400)/53,400 = +0.028%  (below default 0.10% veto)
        data = _make_decision(
            "SELL",
            snapshot_price=53_400.0,
            cpr_tc=53_520.0,
            cpr_bc=53_649.0,  # inverted CPR — matches today's BANKNIFTY shape
        )
        opened, fetched = asyncio.get_event_loop().run_until_complete(
            _run(data, market_ltp=53_400.0, live_ltp=53_415.0)
        )
        fetched.assert_awaited_once_with(SYMBOL)
        opened.assert_not_called()

    def test_cpr_gate_passes_when_both_prices_below_threshold(self):
        """Control: both snapshot and live are below threshold — no leak,
        no false-block. Gate correctly passes."""
        data = _make_decision(
            "SELL",
            snapshot_price=53_300.0,
            cpr_tc=53_520.0,
            cpr_bc=53_649.0,
        )
        # Live moves slightly but stays well below CPR-lower threshold (53,413)
        opened, _fetched = asyncio.get_event_loop().run_until_complete(
            _run(data, market_ltp=53_300.0, live_ltp=53_310.0)
        )
        opened.assert_called_once()
