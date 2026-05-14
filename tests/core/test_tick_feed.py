"""
Unit tests for FyersTickFeed (core-engine/fyers/tick_feed.py).

Covers the message-filter + coalesce + throttle pipeline without touching the
Fyers SDK or a live WebSocket. The SDK + asyncio bridge is end-to-end tested
manually via tests/fyers_sdk_ws_path_check.py.
"""
import asyncio
import json
import sys
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# Stub config.settings before importing tick_feed.
_stub_settings = SimpleNamespace(
    fyers_client_id="TEST-APP-200",
    redis_url="redis://localhost:6379",
)
_cfg = ModuleType("config")
_cfg.settings = _stub_settings
sys.modules["config"] = _cfg

# Import the real `fyers` package (empty __init__.py) so it's registered as a
# proper package — needed for `from fyers.tick_feed import ...` to work — then
# pre-populate sys.modules['fyers.auth'] with a stub so tick_feed picks ours.
import fyers  # noqa: E402
_auth = ModuleType("fyers.auth")
_auth.get_valid_token = MagicMock(return_value="dummy-jwt-token")
sys.modules["fyers.auth"] = _auth
fyers.auth = _auth  # type: ignore[attr-defined]

# Stub redis.asyncio so we don't pull in the real client.
_redis_mod = ModuleType("redis")
_redis_async = ModuleType("redis.asyncio")
_redis_async.Redis = MagicMock()
_redis_mod.asyncio = _redis_async
sys.modules["redis"] = _redis_mod
sys.modules["redis.asyncio"] = _redis_async

# Stub fyers_apiv3 — tick_feed only imports it lazily inside _connect_blocking,
# but it must be importable for the module-level type checks.
_fyers_apiv3 = ModuleType("fyers_apiv3")
_fyers_ws_pkg = ModuleType("fyers_apiv3.FyersWebsocket")
_fyers_ws_data = ModuleType("fyers_apiv3.FyersWebsocket.data_ws")
_fyers_ws_data.FyersDataSocket = MagicMock()
_fyers_apiv3.FyersWebsocket = _fyers_ws_pkg
_fyers_ws_pkg.data_ws = _fyers_ws_data
sys.modules["fyers_apiv3"] = _fyers_apiv3
sys.modules["fyers_apiv3.FyersWebsocket"] = _fyers_ws_pkg
sys.modules["fyers_apiv3.FyersWebsocket.data_ws"] = _fyers_ws_data

from fyers.tick_feed import FyersTickFeed, _WRITE_THROTTLE_MS  # noqa: E402


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_redis():
    r = MagicMock()
    r.setex = AsyncMock()
    return r


def _msg(symbol: str, ltp: float, msg_type: str = "if", exch_ts: int = 1778767700) -> dict:
    return {
        "type": msg_type,
        "symbol": symbol,
        "ltp": ltp,
        "exch_feed_time": exch_ts,
        "high_price": ltp + 10,
        "low_price": ltp - 10,
        "open_price": ltp - 5,
        "prev_close_price": ltp - 100,
        "ch": 100,
        "chp": 0.5,
    }


def _setex_calls(redis_mock):
    """Return list of (key, ttl, payload_dict) tuples from setex calls."""
    out = []
    for c in redis_mock.setex.call_args_list:
        key = c.args[0]
        ttl = c.args[1]
        payload = json.loads(c.args[2])
        out.append((key, ttl, payload))
    return out


# ── Message filter ─────────────────────────────────────────────────────────────

class TestMessageFilter:
    """on_message should only route 'if' / 'sf' messages — drop everything else."""

    def test_control_message_dropped(self):
        async def _run():
            redis = _make_redis()
            feed = FyersTickFeed(redis, ["NSE:NIFTY50-INDEX"])
            feed._tick_event = asyncio.Event()
            # Control messages from Fyers (cn, ful, sub) — no symbol/ltp
            feed._on_message({"type": "cn", "code": 200, "message": "Authentication done", "s": "ok"})
            feed._on_message({"type": "ful", "code": 200, "message": "Full Mode On", "s": "ok"})
            feed._on_message({"type": "sub", "code": 200, "message": "Subscribed", "s": "ok"})
            assert feed._latest == {}, "control messages should not populate latest dict"

        asyncio.get_event_loop().run_until_complete(_run())

    def test_index_feed_recorded(self):
        async def _run():
            redis = _make_redis()
            feed = FyersTickFeed(redis, ["NSE:NIFTY50-INDEX"])
            feed._tick_event = asyncio.Event()
            feed._on_message(_msg("NSE:NIFTY50-INDEX", 23700.5))
            assert "NSE:NIFTY50-INDEX" in feed._latest
            assert feed._latest["NSE:NIFTY50-INDEX"]["ltp"] == 23700.5

        asyncio.get_event_loop().run_until_complete(_run())

    def test_symbol_feed_recorded(self):
        """'sf' is the equity-feed analogue of 'if' — also has ltp+symbol."""
        async def _run():
            redis = _make_redis()
            feed = FyersTickFeed(redis, ["NSE:RELIANCE-EQ"])
            feed._tick_event = asyncio.Event()
            feed._on_message(_msg("NSE:RELIANCE-EQ", 2900.0, msg_type="sf"))
            assert feed._latest["NSE:RELIANCE-EQ"]["ltp"] == 2900.0

        asyncio.get_event_loop().run_until_complete(_run())

    def test_malformed_message_dropped(self):
        async def _run():
            redis = _make_redis()
            feed = FyersTickFeed(redis, ["NSE:NIFTY50-INDEX"])
            feed._tick_event = asyncio.Event()
            feed._on_message({"type": "if", "symbol": "X"})  # no ltp
            feed._on_message({"type": "if", "ltp": 100})      # no symbol
            feed._on_message("not a dict")
            feed._on_message(None)
            assert feed._latest == {}

        asyncio.get_event_loop().run_until_complete(_run())


# ── Coalesce: newest overwrites older ─────────────────────────────────────────

class TestCoalesce:
    """Multiple ticks for the same symbol should leave only the latest."""

    def test_latest_overwrites_older(self):
        async def _run():
            redis = _make_redis()
            feed = FyersTickFeed(redis, ["NSE:NIFTY50-INDEX"])
            feed._tick_event = asyncio.Event()
            feed._on_message(_msg("NSE:NIFTY50-INDEX", 23700.0))
            feed._on_message(_msg("NSE:NIFTY50-INDEX", 23701.0))
            feed._on_message(_msg("NSE:NIFTY50-INDEX", 23702.0))
            assert feed._latest["NSE:NIFTY50-INDEX"]["ltp"] == 23702.0, \
                "consumer should see only the freshest tick"

        asyncio.get_event_loop().run_until_complete(_run())

    def test_independent_symbols_both_kept(self):
        async def _run():
            redis = _make_redis()
            feed = FyersTickFeed(redis, ["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX"])
            feed._tick_event = asyncio.Event()
            feed._on_message(_msg("NSE:NIFTY50-INDEX", 23700.0))
            feed._on_message(_msg("NSE:NIFTYBANK-INDEX", 54100.0))
            assert feed._latest["NSE:NIFTY50-INDEX"]["ltp"] == 23700.0
            assert feed._latest["NSE:NIFTYBANK-INDEX"]["ltp"] == 54100.0

        asyncio.get_event_loop().run_until_complete(_run())


# ── Consumer + Redis write ─────────────────────────────────────────────────────

class TestConsumerWritesRedis:
    """The async consumer should drain _latest and write ltp:{symbol} to Redis."""

    def test_single_tick_written(self):
        async def _run():
            redis = _make_redis()
            feed = FyersTickFeed(redis, ["NSE:NIFTY50-INDEX"])
            feed._tick_event = asyncio.Event()
            # Populate as the SDK thread would
            feed._latest["NSE:NIFTY50-INDEX"] = {"ltp": 23700.5, "exch_ts": 1778767700}
            feed._tick_event.set()
            # Run the consumer for a brief moment then cancel
            task = asyncio.create_task(feed._consume())
            await asyncio.sleep(0.05)
            feed._stopped = True
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            calls = _setex_calls(redis)
            assert len(calls) == 1
            key, ttl, payload = calls[0]
            assert key == "ltp:NSE:NIFTY50-INDEX"
            assert ttl == 30
            assert payload["ltp"] == 23700.5
            assert payload["exch_ts"] == 1778767700
            assert "ts" in payload and payload["ts"] > 0

        asyncio.get_event_loop().run_until_complete(_run())

    def test_multiple_symbols_each_written_once(self):
        async def _run():
            redis = _make_redis()
            feed = FyersTickFeed(redis, ["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX"])
            feed._tick_event = asyncio.Event()
            feed._latest["NSE:NIFTY50-INDEX"]   = {"ltp": 23700.0, "exch_ts": 1}
            feed._latest["NSE:NIFTYBANK-INDEX"] = {"ltp": 54100.0, "exch_ts": 2}
            feed._tick_event.set()
            task = asyncio.create_task(feed._consume())
            await asyncio.sleep(0.05)
            feed._stopped = True
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            keys = {c[0] for c in _setex_calls(redis)}
            assert keys == {"ltp:NSE:NIFTY50-INDEX", "ltp:NSE:NIFTYBANK-INDEX"}

        asyncio.get_event_loop().run_until_complete(_run())


# ── Throttle ──────────────────────────────────────────────────────────────────

class TestThrottle:
    """Consecutive ticks within the throttle window should be dropped, not written."""

    def test_second_tick_within_window_dropped(self):
        async def _run():
            redis = _make_redis()
            feed = FyersTickFeed(redis, ["NSE:NIFTY50-INDEX"])
            feed._tick_event = asyncio.Event()
            # First write goes through
            feed._latest["NSE:NIFTY50-INDEX"] = {"ltp": 23700.0, "exch_ts": 1}
            feed._tick_event.set()
            task = asyncio.create_task(feed._consume())
            await asyncio.sleep(0.05)

            # Second update right after — well within the 500ms throttle window
            feed._latest["NSE:NIFTY50-INDEX"] = {"ltp": 23701.0, "exch_ts": 2}
            feed._tick_event.set()
            await asyncio.sleep(0.05)

            feed._stopped = True
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            calls = _setex_calls(redis)
            assert len(calls) == 1, \
                f"throttle should have skipped the 2nd write within {_WRITE_THROTTLE_MS}ms"
            assert calls[0][2]["ltp"] == 23700.0

        asyncio.get_event_loop().run_until_complete(_run())

    def test_throttle_per_symbol_independent(self):
        """A throttled SYMBOL_A must not block a fresh tick for SYMBOL_B."""
        async def _run():
            redis = _make_redis()
            feed = FyersTickFeed(redis, ["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX"])
            feed._tick_event = asyncio.Event()
            # Both arrive in the same wake-up — both should be written
            feed._latest["NSE:NIFTY50-INDEX"]   = {"ltp": 23700.0, "exch_ts": 1}
            feed._latest["NSE:NIFTYBANK-INDEX"] = {"ltp": 54100.0, "exch_ts": 2}
            feed._tick_event.set()
            task = asyncio.create_task(feed._consume())
            await asyncio.sleep(0.05)
            feed._stopped = True
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            keys = sorted(c[0] for c in _setex_calls(redis))
            assert keys == ["ltp:NSE:NIFTY50-INDEX", "ltp:NSE:NIFTYBANK-INDEX"]

        asyncio.get_event_loop().run_until_complete(_run())
