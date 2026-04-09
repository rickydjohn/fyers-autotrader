"""
Unit tests for the budget gate added to mock_broker.open_position().

Verifies that when an option's total cost (premium × lot_size after slippage)
exceeds the max position value, the trade is rejected before allocate() is called.

All external dependencies stubbed — no Redis, DB, or Fyers connections.
"""

import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Stubs (must be set before importing mock_broker) ──────────────────────────

def _stub(name, **attrs):
    m = ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


# redis.asyncio
_aioredis = _stub("redis.asyncio", Redis=MagicMock())
_stub("redis", asyncio=_aioredis)

# data_client — all calls are no-ops
_stub("data_client",
      persist_trade=AsyncMock(),
      mark_decision_acted=AsyncMock())

# portfolio.budget — patched per test via patch(); stub the module here
_stub("portfolio.budget",
      allocate=AsyncMock(return_value=True),
      get_max_position_value=AsyncMock(return_value=10_000.0),
      release=AsyncMock())
_stub("portfolio")

from execution.mock_broker import open_position  # noqa: E402
import execution.mock_broker as _mb              # noqa: E402

# ── Helpers ───────────────────────────────────────────────────────────────────

LOT_SIZE  = 75
MAX_VALUE = 10_000.0   # 10% of ₹1,00,000 initial budget
SLIPPAGE  = 0.05 / 100  # matches sim conftest settings


def _option_cost(premium: float) -> float:
    """Total cost after slippage — mirrors mock_broker._apply_slippage for BUY."""
    return premium * (1 + SLIPPAGE) * LOT_SIZE


def _redis() -> AsyncMock:
    r = AsyncMock()
    r.hget.return_value = None      # no existing position
    r.exists.return_value = False   # no SL cooldown
    return r


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestBudgetGate:
    """The new gate: if option_cost > max_value, return None before allocate()."""

    @pytest.mark.asyncio
    async def test_rejects_option_when_cost_exceeds_max_value(self):
        # premium=150 → cost ≈ 150.075 × 75 = ₹11,256 > ₹10,000
        expensive_premium = 150.0
        assert _option_cost(expensive_premium) > MAX_VALUE

        with (
            patch.object(_mb, "get_max_position_value", AsyncMock(return_value=MAX_VALUE)),
            patch.object(_mb, "allocate", AsyncMock(return_value=True)) as mock_alloc,
        ):
            result = await open_position(
                redis_client=_redis(),
                symbol="NSE:NIFTY50-INDEX",
                side="BUY",
                price=24010.0,
                stop_loss=23800.0,
                target=24400.0,
                decision_id="test-decision",
                reasoning="test",
                option_symbol="NSE:NIFTY24000CE",
                option_price=expensive_premium,
                option_lot_size=LOT_SIZE,
            )

        assert result is None
        mock_alloc.assert_not_called()

    @pytest.mark.asyncio
    async def test_allows_option_when_cost_within_max_value(self):
        # premium=100 → cost ≈ 100.05 × 75 = ₹7,504 ≤ ₹10,000
        affordable_premium = 100.0
        assert _option_cost(affordable_premium) <= MAX_VALUE

        # Freeze time at 10:00 IST so the session-close gate (15:20) does not fire
        _dt = MagicMock()
        _dt.now.return_value = MagicMock(hour=10, minute=0)

        with (
            patch("execution.mock_broker.datetime", _dt),
            patch.object(_mb, "get_max_position_value", AsyncMock(return_value=MAX_VALUE)),
            patch.object(_mb, "allocate", AsyncMock(return_value=True)) as mock_alloc,
            patch.object(_mb, "data_client") as mock_dc,
        ):
            mock_dc.persist_trade = AsyncMock()
            mock_dc.mark_decision_acted = AsyncMock()
            result = await open_position(
                redis_client=_redis(),
                symbol="NSE:NIFTY50-INDEX",
                side="BUY",
                price=24010.0,
                stop_loss=23800.0,
                target=24400.0,
                decision_id="test-decision",
                reasoning="test",
                option_symbol="NSE:NIFTY24000CE",
                option_price=affordable_premium,
                option_lot_size=LOT_SIZE,
            )

        # allocate must have been called (trade was not blocked)
        mock_alloc.assert_called_once()

    @pytest.mark.asyncio
    async def test_exactly_at_budget_boundary_is_allowed(self):
        # Find premium where cost == MAX_VALUE exactly
        exact_premium = MAX_VALUE / (LOT_SIZE * (1 + SLIPPAGE))
        assert abs(_option_cost(exact_premium) - MAX_VALUE) < 0.01

        # Freeze time at 10:00 IST so the session-close gate (15:20) does not fire
        _dt = MagicMock()
        _dt.now.return_value = MagicMock(hour=10, minute=0)

        with (
            patch("execution.mock_broker.datetime", _dt),
            patch.object(_mb, "get_max_position_value", AsyncMock(return_value=MAX_VALUE)),
            patch.object(_mb, "allocate", AsyncMock(return_value=True)) as mock_alloc,
            patch.object(_mb, "data_client") as mock_dc,
        ):
            mock_dc.persist_trade = AsyncMock()
            mock_dc.mark_decision_acted = AsyncMock()
            await open_position(
                redis_client=_redis(),
                symbol="NSE:NIFTY50-INDEX",
                side="BUY",
                price=24010.0,
                stop_loss=23800.0,
                target=24400.0,
                decision_id="test-decision",
                reasoning="test",
                option_symbol="NSE:NIFTY24000CE",
                option_price=exact_premium,
                option_lot_size=LOT_SIZE,
            )

        mock_alloc.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_trade_when_get_affordable_option_returns_none(self):
        """When get_affordable_option returns None (no option_symbol), no trade is made.

        Previously the broker fell back to trading the underlying index at the raw
        LTP — this test pins the corrected behaviour: return None without calling
        allocate().
        """
        with (
            patch.object(_mb, "get_max_position_value", AsyncMock(return_value=MAX_VALUE)),
            patch.object(_mb, "allocate", AsyncMock(return_value=True)) as mock_alloc,
        ):
            result = await open_position(
                redis_client=_redis(),
                symbol="NSE:NIFTY50-INDEX",
                side="BUY",
                price=24010.0,        # underlying index LTP — must NOT be used as entry
                stop_loss=23800.0,
                target=24400.0,
                decision_id="test-decision",
                reasoning="test",
                option_symbol=None,   # get_affordable_option returned None
                option_price=None,
                option_lot_size=None,
            )

        assert result is None
        mock_alloc.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_trade_when_option_price_missing(self):
        """option_symbol present but option_price is None/0 — no trade."""
        with (
            patch.object(_mb, "get_max_position_value", AsyncMock(return_value=MAX_VALUE)),
            patch.object(_mb, "allocate", AsyncMock(return_value=True)) as mock_alloc,
        ):
            result = await open_position(
                redis_client=_redis(),
                symbol="NSE:NIFTY50-INDEX",
                side="BUY",
                price=24010.0,
                stop_loss=23800.0,
                target=24400.0,
                decision_id="test-decision",
                reasoning="test",
                option_symbol="NSE:NIFTY24000CE",
                option_price=None,    # price lookup failed after option was selected
                option_lot_size=LOT_SIZE,
            )

        assert result is None
        mock_alloc.assert_not_called()


class TestExistingGatesStillWork:
    """Ensure pre-existing gates (session close, SL cooldown) still block correctly."""

    @pytest.mark.asyncio
    async def test_sl_cooldown_blocks_before_budget_gate(self):
        r = _redis()
        r.exists.return_value = True   # SL cooldown active
        r.ttl.return_value = 120

        with patch.object(_mb, "get_max_position_value", AsyncMock(return_value=MAX_VALUE)):
            result = await open_position(
                redis_client=r,
                symbol="NSE:NIFTY50-INDEX",
                side="BUY",
                price=24010.0,
                stop_loss=23800.0,
                target=24400.0,
                decision_id="test-decision",
                reasoning="test",
                option_symbol="NSE:NIFTY24000CE",
                option_price=100.0,
                option_lot_size=LOT_SIZE,
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_min_premium_gate_blocks_cheap_options(self):
        # min_option_premium = 5.0 (from sim conftest settings)
        r = _redis()

        with patch.object(_mb, "get_max_position_value", AsyncMock(return_value=MAX_VALUE)):
            result = await open_position(
                redis_client=r,
                symbol="NSE:NIFTY50-INDEX",
                side="BUY",
                price=24010.0,
                stop_loss=23800.0,
                target=24400.0,
                decision_id="test-decision",
                reasoning="test",
                option_symbol="NSE:NIFTY24000CE",
                option_price=3.0,   # below min_option_premium of 5.0
                option_lot_size=LOT_SIZE,
            )

        assert result is None
