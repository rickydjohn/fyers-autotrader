"""
Unit tests for portfolio/budget.py — specifically get_max_position_value().

Isolated from test_mock_broker_budget.py so the real portfolio.budget module
is imported instead of the broker test's stub.
"""

import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# ── Minimal stubs so budget.py can be imported ────────────────────────────────

def _stub(name, **attrs):
    m = ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m

_aioredis = _stub("redis.asyncio", Redis=None)
_stub("redis", asyncio=_aioredis)

# config is already set by conftest.py; just make sure it has initial_budget
import config as _cfg
if not hasattr(_cfg.settings, "initial_budget"):
    _cfg.settings.initial_budget = 100_000.0

# Evict any portfolio.budget stub installed by the broker test file so we
# import the real module from simulation-engine/portfolio/budget.py.
sys.modules.pop("portfolio.budget", None)
sys.modules.pop("portfolio", None)

from portfolio.budget import get_max_position_value  # noqa: E402
import portfolio.budget as _budget                   # noqa: E402


def _redis_with_state(initial: float, cash: float, invested: float = 0.0) -> AsyncMock:
    """Return a mock Redis client that serves the given BudgetState from Redis."""
    from models.schemas import BudgetState
    state = BudgetState(initial=initial, cash=cash, invested=invested)
    r = AsyncMock()
    r.get.return_value = json.dumps(state.model_dump())
    return r


class TestGetMaxPositionValue:
    """get_max_position_value() uses state.cash so profits compound into position sizing."""

    @pytest.mark.asyncio
    async def test_uses_current_cash_not_initial_budget(self):
        # capital grew from ₹1L to ₹1.16L via realized P&L
        _budget.settings = SimpleNamespace(max_position_size_pct=85.0)
        r = _redis_with_state(initial=100_000.0, cash=116_000.0)

        result = await get_max_position_value(r)

        assert result == pytest.approx(116_000.0 * 0.85)   # ₹98,600 — not ₹85,000

    @pytest.mark.asyncio
    async def test_initial_capital_unchanged_cash_matches_initial(self):
        # day-1: no trades yet, cash == initial
        _budget.settings = SimpleNamespace(max_position_size_pct=85.0)
        r = _redis_with_state(initial=100_000.0, cash=100_000.0)

        result = await get_max_position_value(r)

        assert result == pytest.approx(85_000.0)

    @pytest.mark.asyncio
    async def test_grows_as_cash_grows(self):
        _budget.settings = SimpleNamespace(max_position_size_pct=85.0)

        for cash, expected in [(100_000.0, 85_000.0), (150_000.0, 127_500.0)]:
            r = _redis_with_state(initial=100_000.0, cash=cash)
            result = await get_max_position_value(r)
            assert result == pytest.approx(expected), f"cash={cash}"

    @pytest.mark.asyncio
    async def test_respects_max_position_size_pct_setting(self):
        # same cash, different pct → different cap
        r = _redis_with_state(initial=100_000.0, cash=116_000.0)

        _budget.settings = SimpleNamespace(max_position_size_pct=50.0)
        assert await get_max_position_value(r) == pytest.approx(58_000.0)

        _budget.settings = SimpleNamespace(max_position_size_pct=85.0)
        assert await get_max_position_value(r) == pytest.approx(98_600.0)
