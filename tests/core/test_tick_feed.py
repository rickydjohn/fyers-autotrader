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


# Same minute boundary as exch_ts=1778767700 (which is 2026-05-14 19:38:20 UTC).
# Minute floor: 1778767680.
_MIN_A = 1778767680     # bar at 19:38 UTC
_MIN_B = 1778767740     # bar at 19:39 UTC


def _setex_calls(redis_mock):
    """Return list of (key, ttl, payload_dict) tuples from setex calls."""
    out = []
    for c in redis_mock.setex.call_args_list:
        key = c.args[0]
        ttl = c.args[1]
        payload = json.loads(c.args[2])
        out.append((key, ttl, payload))
    return out


# ── Watchdog (freshness self-heal) ──────────────────────────────────────────

class TestWatchdog:
    """Independent freshness watchdog — the 2026-05-29 zombie fix. On a stale feed
    during market hours it must tear down the SDK and request a reconnect, on its
    own task (so it fires even when connect() never returns)."""

    def test_stale_feed_forces_reconnect(self, monkeypatch):
        import fyers.tick_feed as tf
        monkeypatch.setattr(tf, "_is_market_hours", lambda: True)
        monkeypatch.setattr(tf, "_HEALTH_CHECK_INTERVAL_S", 0.01)

        async def _run():
            feed = FyersTickFeed(_make_redis(), ["NSE:NIFTY50-INDEX"])
            feed._reconnect_requested = asyncio.Event()
            feed._fyers = MagicMock()                          # a live SDK to tear down
            feed._last_msg_monotonic = time.monotonic() - 999  # very stale
            task = asyncio.ensure_future(feed._watchdog())
            try:
                await asyncio.wait_for(feed._reconnect_requested.wait(), timeout=1.0)
            finally:
                feed._stopped = True
                task.cancel()
            assert feed._reconnect_requested.is_set()
            assert feed._fyers is None                         # torn down

        asyncio.get_event_loop().run_until_complete(_run())

    def test_no_reconnect_outside_market_hours(self, monkeypatch):
        import fyers.tick_feed as tf
        monkeypatch.setattr(tf, "_is_market_hours", lambda: False)
        monkeypatch.setattr(tf, "_HEALTH_CHECK_INTERVAL_S", 0.01)

        async def _run():
            feed = FyersTickFeed(_make_redis(), ["NSE:NIFTY50-INDEX"])
            feed._reconnect_requested = asyncio.Event()
            feed._fyers = MagicMock()
            feed._last_msg_monotonic = time.monotonic() - 999
            task = asyncio.ensure_future(feed._watchdog())
            await asyncio.sleep(0.1)
            feed._stopped = True
            task.cancel()
            assert not feed._reconnect_requested.is_set()      # silence OK off-hours
            assert feed._fyers is not None

        asyncio.get_event_loop().run_until_complete(_run())


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


# ── Forming-bar accumulation ─────────────────────────────────────────────────

class TestFormingBarAccumulation:
    """Per-minute OHLC accumulator: open=first tick, high/low=running extremes,
    close=latest tick. Minute boundary derived from exch_feed_time (epoch s)."""

    def test_first_tick_of_minute_sets_ohlc_to_ltp(self):
        async def _run():
            redis = _make_redis()
            feed = FyersTickFeed(redis, ["NSE:NIFTY50-INDEX"])
            feed._tick_event = asyncio.Event()
            feed._on_message(_msg("NSE:NIFTY50-INDEX", 23700.5, exch_ts=_MIN_A + 10))
            fb = feed._forming_bars["NSE:NIFTY50-INDEX"]
            assert fb["bar_min"] == _MIN_A
            assert fb["open"] == fb["high"] == fb["low"] == fb["close"] == 23700.5
            assert fb["n"] == 1
        asyncio.get_event_loop().run_until_complete(_run())

    def test_subsequent_ticks_update_hlc_only(self):
        async def _run():
            redis = _make_redis()
            feed = FyersTickFeed(redis, ["NSE:NIFTY50-INDEX"])
            feed._tick_event = asyncio.Event()
            feed._on_message(_msg("NSE:NIFTY50-INDEX", 23700.0, exch_ts=_MIN_A + 5))
            feed._on_message(_msg("NSE:NIFTY50-INDEX", 23705.0, exch_ts=_MIN_A + 20))   # new high
            feed._on_message(_msg("NSE:NIFTY50-INDEX", 23695.0, exch_ts=_MIN_A + 40))   # new low
            feed._on_message(_msg("NSE:NIFTY50-INDEX", 23702.5, exch_ts=_MIN_A + 55))   # close
            fb = feed._forming_bars["NSE:NIFTY50-INDEX"]
            assert fb["bar_min"] == _MIN_A
            assert fb["open"]  == 23700.0
            assert fb["high"]  == 23705.0
            assert fb["low"]   == 23695.0
            assert fb["close"] == 23702.5
            assert fb["n"]     == 4
        asyncio.get_event_loop().run_until_complete(_run())

    def test_minute_rollover_finalises_previous_starts_new(self):
        async def _run():
            redis = _make_redis()
            feed = FyersTickFeed(redis, ["NSE:NIFTY50-INDEX"])
            feed._tick_event = asyncio.Event()
            feed._on_message(_msg("NSE:NIFTY50-INDEX", 23700.0, exch_ts=_MIN_A + 30))
            feed._on_message(_msg("NSE:NIFTY50-INDEX", 23705.0, exch_ts=_MIN_A + 50))
            feed._on_message(_msg("NSE:NIFTY50-INDEX", 23708.0, exch_ts=_MIN_B + 1))    # rollover
            # Previous bar should be in _finalized_bars and removed-but-replaced in _forming_bars
            assert "NSE:NIFTY50-INDEX" in feed._finalized_bars
            done = feed._finalized_bars["NSE:NIFTY50-INDEX"]
            assert done["bar_min"] == _MIN_A
            assert done["open"]  == 23700.0
            assert done["high"]  == 23705.0
            assert done["low"]   == 23700.0
            assert done["close"] == 23705.0
            assert done["n"]     == 2
            new_fb = feed._forming_bars["NSE:NIFTY50-INDEX"]
            assert new_fb["bar_min"] == _MIN_B
            assert new_fb["open"] == 23708.0
            assert new_fb["n"] == 1
        asyncio.get_event_loop().run_until_complete(_run())

    def test_independent_symbols_each_track_their_own_bar(self):
        async def _run():
            redis = _make_redis()
            feed = FyersTickFeed(redis, ["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX"])
            feed._tick_event = asyncio.Event()
            feed._on_message(_msg("NSE:NIFTY50-INDEX",    23700.0, exch_ts=_MIN_A + 10))
            feed._on_message(_msg("NSE:NIFTYBANK-INDEX",  54100.0, exch_ts=_MIN_A + 10))
            feed._on_message(_msg("NSE:NIFTY50-INDEX",    23710.0, exch_ts=_MIN_A + 30))
            assert feed._forming_bars["NSE:NIFTY50-INDEX"]["high"] == 23710.0
            assert feed._forming_bars["NSE:NIFTYBANK-INDEX"]["high"] == 54100.0
        asyncio.get_event_loop().run_until_complete(_run())

    def test_missing_exch_ts_skips_forming_bar(self):
        """Without exch_feed_time we have no reliable minute boundary, so the
        bar logic skips that tick (ltp:* is still updated)."""
        async def _run():
            redis = _make_redis()
            feed = FyersTickFeed(redis, ["NSE:NIFTY50-INDEX"])
            feed._tick_event = asyncio.Event()
            msg = _msg("NSE:NIFTY50-INDEX", 23700.0)
            msg.pop("exch_feed_time")
            feed._on_message(msg)
            assert feed._forming_bars == {}
            assert feed._latest["NSE:NIFTY50-INDEX"]["ltp"] == 23700.0
        asyncio.get_event_loop().run_until_complete(_run())


# ── Consumer persists forming-bar + last-bar to Redis ────────────────────────

class TestFormingBarPersistence:

    def test_consumer_writes_forming_bar_key(self):
        async def _run():
            redis = _make_redis()
            feed = FyersTickFeed(redis, ["NSE:NIFTY50-INDEX"])
            feed._tick_event = asyncio.Event()
            feed._latest["NSE:NIFTY50-INDEX"] = {"ltp": 23700.0, "exch_ts": _MIN_A + 10}
            feed._forming_bars["NSE:NIFTY50-INDEX"] = {
                "bar_min": _MIN_A, "open": 23700, "high": 23705,
                "low": 23700, "close": 23703, "n": 5,
            }
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
            assert "forming_bar:NSE:NIFTY50-INDEX" in keys
            # Find the forming-bar payload and verify shape
            for key, ttl, payload in _setex_calls(redis):
                if key == "forming_bar:NSE:NIFTY50-INDEX":
                    assert payload["open"]  == 23700
                    assert payload["high"]  == 23705
                    assert payload["close"] == 23703
                    assert payload["n_ticks"] == 5
                    assert payload["time"].endswith("+00:00")  # UTC ISO
                    assert ttl == 90
                    break
        asyncio.get_event_loop().run_until_complete(_run())

    def test_consumer_writes_last_bar_on_rollover(self):
        async def _run():
            redis = _make_redis()
            feed = FyersTickFeed(redis, ["NSE:NIFTY50-INDEX"])
            feed._tick_event = asyncio.Event()
            # Simulate a just-finalised previous bar
            feed._finalized_bars["NSE:NIFTY50-INDEX"] = {
                "bar_min": _MIN_A, "open": 23690, "high": 23700,
                "low": 23685, "close": 23698, "n": 12,
            }
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
            assert "last_bar:NSE:NIFTY50-INDEX" in keys
            for key, ttl, payload in _setex_calls(redis):
                if key == "last_bar:NSE:NIFTY50-INDEX":
                    assert payload["close"] == 23698
                    assert ttl == 120
                    break
        asyncio.get_event_loop().run_until_complete(_run())


# ── Dynamic subscribe / unsubscribe / reconcile ──────────────────────────────

class TestDynamicSubscription:
    """subscribe_symbol / unsubscribe_symbol / reconcile_subscriptions —
    the surface that simulation-engine's open_position/close_position calls
    via the /ws/subscribe and /ws/unsubscribe endpoints."""

    def test_initial_subscribed_set_is_underlyings_only(self):
        feed = FyersTickFeed(_make_redis(), ["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX"])
        assert feed._subscribed == {"NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX"}

    def test_subscribe_symbol_adds(self):
        feed = FyersTickFeed(_make_redis(), ["NSE:NIFTY50-INDEX"])
        added = feed.subscribe_symbol("NSE:NIFTY26MAY24300CE")
        assert added is True
        assert "NSE:NIFTY26MAY24300CE" in feed._subscribed

    def test_subscribe_symbol_is_idempotent(self):
        feed = FyersTickFeed(_make_redis(), ["NSE:NIFTY50-INDEX"])
        feed.subscribe_symbol("NSE:NIFTY26MAY24300CE")
        added_again = feed.subscribe_symbol("NSE:NIFTY26MAY24300CE")
        assert added_again is False  # already present
        assert len([s for s in feed._subscribed if "24300CE" in s]) == 1

    def test_unsubscribe_symbol_removes_option(self):
        feed = FyersTickFeed(_make_redis(), ["NSE:NIFTY50-INDEX"])
        feed.subscribe_symbol("NSE:NIFTY26MAY24300CE")
        removed = feed.unsubscribe_symbol("NSE:NIFTY26MAY24300CE")
        assert removed is True
        assert "NSE:NIFTY26MAY24300CE" not in feed._subscribed

    def test_unsubscribe_refuses_to_remove_underlying(self):
        """Underlyings are required by the scan + forming-bar and must
        survive any unsubscribe call."""
        feed = FyersTickFeed(_make_redis(), ["NSE:NIFTY50-INDEX"])
        removed = feed.unsubscribe_symbol("NSE:NIFTY50-INDEX")
        assert removed is False
        assert "NSE:NIFTY50-INDEX" in feed._subscribed

    def test_unsubscribe_unknown_symbol_is_noop(self):
        feed = FyersTickFeed(_make_redis(), ["NSE:NIFTY50-INDEX"])
        assert feed.unsubscribe_symbol("NSE:DOES-NOT-EXIST") is False

    def test_reconcile_subscribes_missing_options(self):
        """When the periodic reconcile sees positions:open contains an
        option we haven't subscribed to, add it."""
        feed = FyersTickFeed(_make_redis(), ["NSE:NIFTY50-INDEX"])
        result = feed.reconcile_subscriptions(["NSE:NIFTY26MAY24300CE"])
        assert "NSE:NIFTY26MAY24300CE" in feed._subscribed
        assert result["added"] == ["NSE:NIFTY26MAY24300CE"]
        assert result["removed"] == []

    def test_reconcile_unsubscribes_stale_options(self):
        """A position closed but unsubscribe didn't reach us — the next
        reconcile should drop the orphan subscription."""
        feed = FyersTickFeed(_make_redis(), ["NSE:NIFTY50-INDEX"])
        feed.subscribe_symbol("NSE:NIFTY26MAY24300CE")    # an open position
        feed.subscribe_symbol("NSE:NIFTY26MAY24200PE")    # another open position
        # Now positions:open shows only the CE — the PE was closed.
        result = feed.reconcile_subscriptions(["NSE:NIFTY26MAY24300CE"])
        assert "NSE:NIFTY26MAY24200PE" not in feed._subscribed
        assert result["removed"] == ["NSE:NIFTY26MAY24200PE"]
        assert result["added"] == []

    def test_reconcile_preserves_underlyings(self):
        feed = FyersTickFeed(_make_redis(), ["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX"])
        feed.subscribe_symbol("NSE:NIFTY26MAY24300CE")
        feed.reconcile_subscriptions([])   # no open positions
        assert feed._subscribed == {"NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX"}

    def test_reconcile_empty_target_set(self):
        """Edge case: reconcile to ∅ — underlyings should still survive."""
        feed = FyersTickFeed(_make_redis(), ["NSE:NIFTY50-INDEX"])
        feed.subscribe_symbol("NSE:NIFTY26MAY24300CE")
        result = feed.reconcile_subscriptions([])
        assert feed._subscribed == {"NSE:NIFTY50-INDEX"}
        assert result["removed"] == ["NSE:NIFTY26MAY24300CE"]
