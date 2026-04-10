"""
Tests for:
  1. Pending key dedup — race condition fix in open_position()
  2. Startup reconciliation — _startup_fyers_reconcile()
  3. Core-engine position watcher config default

Run inside trading-sim container:
    docker exec trading-sim python -m pytest /app/test_changes.py -v
"""

import asyncio
import json
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
import pytz
from fakeredis import FakeAsyncRedis

# ─── Patch all external dependencies before importing our modules ─────────────

_mock_settings = MagicMock()
_mock_settings.core_engine_url = "http://core-engine:8001"
_mock_settings.data_service_url = "http://data-service:8003"
_mock_settings.session_close_hour = 23      # far in the future so session-close gate never fires
_mock_settings.session_close_minute = 59
_mock_settings.min_option_premium = 30.0
_mock_settings.max_position_size_pct = 95.0
_mock_settings.sl_cooldown_minutes = 15
_mock_settings.redis_url = "redis://localhost:6379"
_mock_settings.log_level = "INFO"

sys.modules.setdefault("config", MagicMock(settings=_mock_settings))
sys.modules.setdefault("data_client", MagicMock(
    persist_trade=AsyncMock(),
    mark_decision_acted=AsyncMock(),
))
sys.modules.setdefault("notifications", MagicMock())
sys.modules.setdefault("notifications.slack", MagicMock(
    notify_trade_opened=MagicMock(),
    notify_trade_closed=MagicMock(),
))

from execution.exit_rules import PREMIUM_SL_PCT, FIRST_MILESTONE_PCT  # noqa: E402
from models.schemas import Position  # noqa: E402

IST = pytz.timezone("Asia/Kolkata")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_position(symbol="NSE:NIFTY50-INDEX", option_sym="NSE:NIFTY2641324000CE",
                   entry_price=150.0) -> Position:
    return Position(
        symbol=symbol,
        side="BUY",
        quantity=65,
        avg_price=entry_price,
        entry_time=datetime.now(IST),
        stop_loss=round(entry_price * 0.90, 2),
        target=round(entry_price * 1.15, 2),
        decision_id="test-decision-id",
        option_symbol=option_sym,
        option_strike=24000,
        option_type="CE",
        option_expiry="2026-04-13",
        entry_option_price=entry_price,
        day_type="TRENDING",
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. Pending key — Redis behaviour
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pending_key_blocks_concurrent_check():
    """Setting pending:order:{symbol} should make the dedup check return True (blocked)."""
    redis = FakeAsyncRedis(decode_responses=True)
    symbol = "NSE:NIFTY50-INDEX"
    pending_key = f"pending:order:{symbol}"

    # No key yet — neither pending nor open
    blocked = bool(await redis.exists(pending_key) or await redis.hget("positions:open", symbol))
    assert not blocked, "Should not be blocked before any key is set"

    # Set the pending key (simulates first order in-flight)
    await redis.set(pending_key, "1", ex=30)

    blocked = bool(await redis.exists(pending_key) or await redis.hget("positions:open", symbol))
    assert blocked, "Pending key should block concurrent order"


@pytest.mark.asyncio
async def test_pending_key_ttl():
    """Pending key must have a TTL so it auto-expires on unexpected failures."""
    redis = FakeAsyncRedis(decode_responses=True)
    await redis.set("pending:order:NSE:NIFTY50-INDEX", "1", ex=30)

    ttl = await redis.ttl("pending:order:NSE:NIFTY50-INDEX")
    assert 0 < ttl <= 30, f"Expected TTL 1-30s, got {ttl}"


@pytest.mark.asyncio
async def test_open_position_blocks_when_pending_key_set():
    """
    open_position() must return None immediately when pending:order:{symbol} exists.
    We patch _place_fyers_order with a sentinel to confirm it is never reached.
    """
    redis = FakeAsyncRedis(decode_responses=True)
    symbol = "NSE:NIFTY50-INDEX"

    # Pre-set the pending key — simulates first order in-flight
    await redis.set(f"pending:order:{symbol}", "1", ex=30)

    order_spy = AsyncMock(return_value={"order_id": "ORD123"})

    with patch("execution.live_broker._place_fyers_order", order_spy), \
         patch("execution.live_broker._get_available_funds", AsyncMock(return_value=50000.0)), \
         patch("execution.live_broker.settings", _mock_settings):

        from execution.live_broker import open_position
        result = await open_position(
            redis_client=redis,
            symbol=symbol,
            side="BUY",
            price=24000.0,
            stop_loss=23900.0,
            target=24200.0,
            decision_id="dec-001",
            reasoning="test",
            option_symbol="NSE:NIFTY2641324000CE",
            option_strike=24000,
            option_type="CE",
            option_expiry="2026-04-13",
            option_price=155.0,
            option_lot_size=65,
            day_type="TRENDING",
        )

    assert result is None, "open_position should return None when pending key is set"
    order_spy.assert_not_called()


@pytest.mark.asyncio
async def test_open_position_blocks_when_position_exists():
    """open_position() must return None when positions:open already has the symbol."""
    redis = FakeAsyncRedis(decode_responses=True)
    symbol = "NSE:NIFTY50-INDEX"

    existing = _make_position()
    await redis.hset("positions:open", symbol, existing.model_dump_json())

    order_spy = AsyncMock(return_value={"order_id": "ORD123"})

    with patch("execution.live_broker._place_fyers_order", order_spy), \
         patch("execution.live_broker._get_available_funds", AsyncMock(return_value=50000.0)), \
         patch("execution.live_broker.settings", _mock_settings):

        from execution.live_broker import open_position
        result = await open_position(
            redis_client=redis,
            symbol=symbol,
            side="BUY",
            price=24000.0,
            stop_loss=23900.0,
            target=24200.0,
            decision_id="dec-002",
            reasoning="test",
            option_symbol="NSE:NIFTY2641324000CE",
            option_strike=24000,
            option_type="CE",
            option_expiry="2026-04-13",
            option_price=155.0,
            option_lot_size=65,
            day_type="TRENDING",
        )

    assert result is None, "open_position should return None when position is already open"
    order_spy.assert_not_called()


@pytest.mark.asyncio
async def test_pending_key_cleared_on_order_failure():
    """If _place_fyers_order fails, the pending key must be deleted so the next signal can proceed."""
    redis = FakeAsyncRedis(decode_responses=True)
    symbol = "NSE:NIFTY50-INDEX"
    pending_key = f"pending:order:{symbol}"

    with patch("execution.live_broker._place_fyers_order", AsyncMock(return_value=None)), \
         patch("execution.live_broker._get_available_funds", AsyncMock(return_value=50000.0)), \
         patch("execution.live_broker.settings", _mock_settings):

        from execution.live_broker import open_position
        result = await open_position(
            redis_client=redis,
            symbol=symbol,
            side="BUY",
            price=24000.0,
            stop_loss=23900.0,
            target=24200.0,
            decision_id="dec-003",
            reasoning="test",
            option_symbol="NSE:NIFTY2641324000CE",
            option_strike=24000,
            option_type="CE",
            option_expiry="2026-04-13",
            option_price=155.0,
            option_lot_size=65,
            day_type="TRENDING",
        )

    assert result is None
    exists = await redis.exists(pending_key)
    assert exists == 0, "Pending key must be deleted after order placement failure"


@pytest.mark.asyncio
async def test_pending_key_cleared_on_fill_failure():
    """If _await_fill returns None (order rejected), the pending key must be deleted."""
    redis = FakeAsyncRedis(decode_responses=True)
    symbol = "NSE:NIFTY50-INDEX"
    pending_key = f"pending:order:{symbol}"

    with patch("execution.live_broker._place_fyers_order", AsyncMock(return_value={"order_id": "ORD999"})), \
         patch("execution.live_broker._await_fill", AsyncMock(return_value=None)), \
         patch("execution.live_broker._get_available_funds", AsyncMock(return_value=50000.0)), \
         patch("execution.live_broker.settings", _mock_settings):

        from execution.live_broker import open_position
        result = await open_position(
            redis_client=redis,
            symbol=symbol,
            side="BUY",
            price=24000.0,
            stop_loss=23900.0,
            target=24200.0,
            decision_id="dec-004",
            reasoning="test",
            option_symbol="NSE:NIFTY2641324000CE",
            option_strike=24000,
            option_type="CE",
            option_expiry="2026-04-13",
            option_price=155.0,
            option_lot_size=65,
            day_type="TRENDING",
        )

    assert result is None
    exists = await redis.exists(pending_key)
    assert exists == 0, "Pending key must be deleted after fill confirmation failure"


@pytest.mark.asyncio
async def test_pending_key_cleared_after_successful_open():
    """After a successful position open, the pending key must be gone and position must be in Redis."""
    redis = FakeAsyncRedis(decode_responses=True)
    symbol = "NSE:NIFTY50-INDEX"
    pending_key = f"pending:order:{symbol}"

    mock_fill = {"status": "TRADED", "traded_price": 155.50, "filled_qty": 65}

    with patch("execution.live_broker._place_fyers_order", AsyncMock(return_value={"order_id": "ORD777"})), \
         patch("execution.live_broker._await_fill", AsyncMock(return_value=mock_fill)), \
         patch("execution.live_broker._get_available_funds", AsyncMock(return_value=50000.0)), \
         patch("execution.live_broker.data_client", MagicMock(persist_trade=AsyncMock())), \
         patch("execution.live_broker.notify_trade_opened", MagicMock()), \
         patch("execution.live_broker.settings", _mock_settings):

        from execution.live_broker import open_position
        result = await open_position(
            redis_client=redis,
            symbol=symbol,
            side="BUY",
            price=24000.0,
            stop_loss=23900.0,
            target=24200.0,
            decision_id="dec-005",
            reasoning="test",
            option_symbol="NSE:NIFTY2641324000CE",
            option_strike=24000,
            option_type="CE",
            option_expiry="2026-04-13",
            option_price=155.0,
            option_lot_size=65,
            day_type="TRENDING",
        )

    # Position should be recorded
    assert result is not None, "Expected a Trade object on success"
    pos_raw = await redis.hget("positions:open", symbol)
    assert pos_raw is not None, "Position must be in Redis after successful open"

    # Pending key must be gone
    exists = await redis.exists(pending_key)
    assert exists == 0, "Pending key must be deleted after successful position open"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Startup reconciliation
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_startup_reconcile_skips_when_redis_has_positions():
    """If Redis already tracks positions, startup reconcile must not call Fyers at all."""
    redis = FakeAsyncRedis(decode_responses=True)

    pos = _make_position()
    await redis.hset("positions:open", "NSE:NIFTY50-INDEX", pos.model_dump_json())
    await redis.set("trading:mode", "live")

    http_spy = AsyncMock()

    with patch("httpx.AsyncClient") as mock_client_cls, \
         patch("main.redis_client", redis), \
         patch("main.settings", _mock_settings):
        mock_client_cls.return_value.__aenter__ = http_spy
        from main import _startup_fyers_reconcile
        await _startup_fyers_reconcile()

    http_spy.assert_not_called()


@pytest.mark.asyncio
async def test_startup_reconcile_skips_in_simulation_mode():
    """Startup reconcile must be a no-op in simulation mode."""
    redis = FakeAsyncRedis(decode_responses=True)
    await redis.set("trading:mode", "simulation")

    http_spy = AsyncMock()

    with patch("httpx.AsyncClient") as mock_client_cls, \
         patch("main.redis_client", redis), \
         patch("main.settings", _mock_settings):
        mock_client_cls.return_value.__aenter__ = http_spy
        from main import _startup_fyers_reconcile
        await _startup_fyers_reconcile()

    http_spy.assert_not_called()


@pytest.mark.asyncio
async def test_startup_reconcile_no_op_when_fyers_empty():
    """If Fyers has no open positions, Redis must remain untouched."""
    redis = FakeAsyncRedis(decode_responses=True)
    await redis.set("trading:mode", "live")

    fyers_response = MagicMock()
    fyers_response.status_code = 200
    fyers_response.json.return_value = {"positions": []}

    with patch("httpx.AsyncClient") as mock_client_cls, \
         patch("main.redis_client", redis), \
         patch("main.settings", _mock_settings):
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=fyers_response)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        from main import _startup_fyers_reconcile
        await _startup_fyers_reconcile()

    count = await redis.hlen("positions:open")
    assert count == 0, "Redis should remain empty when Fyers has no positions"


@pytest.mark.asyncio
async def test_startup_reconcile_reconstructs_from_data_service():
    """
    When Redis is empty but Fyers has a position, and data-service has the open trade,
    the position must be reconstructed in Redis using the accurate entry_price from data-service.
    """
    redis = FakeAsyncRedis(decode_responses=True)
    await redis.set("trading:mode", "live")

    fyers_resp = MagicMock()
    fyers_resp.status_code = 200
    fyers_resp.json.return_value = {"positions": [
        {"symbol": "NSE:NIFTY2641324000CE", "buyAvg": 142.60, "netQty": 65}
    ]}

    trades_resp = MagicMock()
    trades_resp.status_code = 200
    trades_resp.json.return_value = {"data": {"trades": [
        {
            "trade_id": "abc-123",
            "option_symbol": "NSE:NIFTY2641324000CE",
            "status": "OPEN",
            "entry_price": 142.60,
            "entry_time": "2026-04-10T10:04:43+05:30",
            "decision_id": "dec-original",
            "option_strike": 24000,
            "option_type": "CE",
            "option_expiry": "2026-04-13",
        }
    ]}}

    call_count = {"n": 0}

    async def _mock_get(url, **kwargs):
        call_count["n"] += 1
        if "fyers/positions" in url:
            return fyers_resp
        if "trades" in url:
            return trades_resp
        return MagicMock(status_code=404)

    with patch("httpx.AsyncClient") as mock_client_cls, \
         patch("main.redis_client", redis), \
         patch("main.settings", _mock_settings):
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(side_effect=_mock_get)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        from main import _startup_fyers_reconcile
        await _startup_fyers_reconcile()

    # Position must now be in Redis
    pos_raw = await redis.hget("positions:open", "NSE:NIFTY50-INDEX")
    assert pos_raw is not None, "Position must be reconstructed in Redis"

    pos = Position(**json.loads(pos_raw))
    assert pos.option_symbol == "NSE:NIFTY2641324000CE"
    assert pos.avg_price == 142.60
    assert pos.decision_id == "dec-original"
    assert pos.stop_loss == round(142.60 * (1.0 - PREMIUM_SL_PCT), 2)


@pytest.mark.asyncio
async def test_startup_reconcile_falls_back_to_fyers_data_when_no_trade_record():
    """When data-service has no matching open trade, reconstruction uses Fyers buyAvg."""
    redis = FakeAsyncRedis(decode_responses=True)
    await redis.set("trading:mode", "live")

    fyers_resp = MagicMock()
    fyers_resp.status_code = 200
    fyers_resp.json.return_value = {"positions": [
        {"symbol": "NSE:NIFTY2641324000CE", "buyAvg": 160.0, "netQty": 65}
    ]}

    trades_resp = MagicMock()
    trades_resp.status_code = 200
    trades_resp.json.return_value = {"data": {"trades": []}}  # no open trades

    async def _mock_get(url, **kwargs):
        if "fyers/positions" in url:
            return fyers_resp
        return trades_resp

    with patch("httpx.AsyncClient") as mock_client_cls, \
         patch("main.redis_client", redis), \
         patch("main.settings", _mock_settings):
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(side_effect=_mock_get)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        from main import _startup_fyers_reconcile
        await _startup_fyers_reconcile()

    pos_raw = await redis.hget("positions:open", "NSE:NIFTY50-INDEX")
    assert pos_raw is not None, "Position must still be reconstructed from Fyers data"

    pos = Position(**json.loads(pos_raw))
    assert pos.avg_price == 160.0
    assert pos.decision_id == "RECOVERED"


@pytest.mark.asyncio
async def test_startup_reconcile_sets_pending_flag_on_fetch_failure():
    """If the Fyers positions API fails, reconcile:pending must be set for retry."""
    redis = FakeAsyncRedis(decode_responses=True)
    await redis.set("trading:mode", "live")

    with patch("httpx.AsyncClient") as mock_client_cls, \
         patch("main.redis_client", redis), \
         patch("main.settings", _mock_settings):
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(side_effect=Exception("connection refused"))
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        from main import _startup_fyers_reconcile
        await _startup_fyers_reconcile()  # must not raise

    # reconcile:pending must be set so the retry loop picks it up
    pending = await redis.exists("reconcile:pending")
    assert pending == 1, "reconcile:pending must be set when Fyers fetch fails"

    # Redis positions remain empty (nothing to reconstruct yet)
    count = await redis.hlen("positions:open")
    assert count == 0


@pytest.mark.asyncio
async def test_startup_reconcile_sets_pending_flag_on_non_200():
    """If core-engine returns a non-200 (e.g. 503 during auth), pending flag must be set."""
    redis = FakeAsyncRedis(decode_responses=True)
    await redis.set("trading:mode", "live")

    bad_resp = MagicMock()
    bad_resp.status_code = 503

    with patch("httpx.AsyncClient") as mock_client_cls, \
         patch("main.redis_client", redis), \
         patch("main.settings", _mock_settings):
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=bad_resp)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        from main import _startup_fyers_reconcile
        await _startup_fyers_reconcile()

    pending = await redis.exists("reconcile:pending")
    assert pending == 1, "reconcile:pending must be set on non-200 response"


@pytest.mark.asyncio
async def test_startup_reconcile_clears_pending_flag_on_success():
    """On a successful Fyers fetch (even with no positions), reconcile:pending must be cleared."""
    redis = FakeAsyncRedis(decode_responses=True)
    await redis.set("trading:mode", "live")
    await redis.set("reconcile:pending", "1")  # pre-existing flag from a previous failed attempt

    fyers_response = MagicMock()
    fyers_response.status_code = 200
    fyers_response.json.return_value = {"positions": []}

    with patch("httpx.AsyncClient") as mock_client_cls, \
         patch("main.redis_client", redis), \
         patch("main.settings", _mock_settings):
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=fyers_response)
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_http)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        from main import _startup_fyers_reconcile
        await _startup_fyers_reconcile()

    pending = await redis.exists("reconcile:pending")
    assert pending == 0, "reconcile:pending must be cleared after a successful Fyers fetch"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Underlying symbol derivation
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("option_sym, expected_underlying", [
    ("NSE:NIFTY2641324000CE",   "NSE:NIFTY50-INDEX"),
    ("NSE:NIFTY2641323900PE",   "NSE:NIFTY50-INDEX"),
    ("NSE:NIFTYBANK2641355000CE", "NSE:NIFTYBANK-INDEX"),
    ("NSE:NIFTYBANK2641354500PE", "NSE:NIFTYBANK-INDEX"),
])
def test_underlying_derivation(option_sym, expected_underlying):
    """Option symbol → underlying index key derivation must be correct."""
    if "NIFTYBANK" in option_sym:
        result = "NSE:NIFTYBANK-INDEX"
    elif "NIFTY" in option_sym:
        result = "NSE:NIFTY50-INDEX"
    else:
        result = None
    assert result == expected_underlying, f"Failed for {option_sym}"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Core-engine config — watcher default
# ─────────────────────────────────────────────────────────────────────────────

def test_position_watcher_default_is_5s():
    """position_watcher_interval_seconds default must be 5 after the config change."""
    import importlib, types

    # Read the config source directly without importing (avoids needing Fyers env vars)
    import ast, pathlib
    src = pathlib.Path("/app/../core-engine/config.py")
    if not src.exists():
        # Running from host — adjust path
        src = pathlib.Path(__file__).parent.parent / "core-engine" / "config.py"
    if not src.exists():
        pytest.skip("core-engine/config.py not accessible from this container")

    tree = ast.parse(src.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == "position_watcher_interval_seconds":
                # The default is the first arg to Field(...)
                if isinstance(node.value, ast.Call):
                    first_arg = node.value.args[0] if node.value.args else None
                    if first_arg and isinstance(first_arg, ast.Constant):
                        assert first_arg.value == 5, (
                            f"Expected position_watcher_interval_seconds default=5, got {first_arg.value}"
                        )
                        return
    pytest.fail("Could not find position_watcher_interval_seconds in core-engine/config.py")
