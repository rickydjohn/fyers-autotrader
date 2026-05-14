"""
Fyers WebSocket tick feed.

Subscribes to underlying symbols via FyersDataSocket and pushes LTP into Redis
as `ltp:{symbol}` keys. Consumers (drift veto, fast position watcher, future
tick-driven exits) read those keys instead of waiting on the 60s REST scan.

Architecture:
  - The SDK runs `WebSocketApp.run_forever` in its own thread (managed inside
    the SDK). on_message() is called from that thread.
  - We bridge SDK-thread → asyncio by stashing the latest tick per symbol in
    a plain dict and signaling an asyncio.Event via call_soon_threadsafe.
  - A single async consumer drains the dict, throttles per symbol, and writes
    to Redis. Coalescing is implicit: only the latest tick per symbol is
    kept while the consumer catches up.

Backpressure model:
  - We don't queue every tick — only the latest per symbol is retained.
  - If the consumer is slow, intermediate ticks are silently dropped (only
    the most recent matters for entry/exit decisions).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Optional

import redis.asyncio as aioredis

from config import settings
from fyers.auth import get_valid_token

logger = logging.getLogger(__name__)


# Tunables — modest defaults, tune after a live-market sampling.
_LTP_REDIS_TTL_S = 30
_WRITE_THROTTLE_MS = 500  # at most one write per symbol per 500ms
_BACKOFF_INITIAL_S = 1.0
_BACKOFF_MAX_S = 60.0


class FyersTickFeed:
    """Maintains a Fyers WS connection and pushes ticks to Redis."""

    def __init__(
        self,
        redis_client: aioredis.Redis,
        symbols: list[str],
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        self._redis = redis_client
        self._symbols = list(symbols)
        self._loop = loop or asyncio.get_event_loop()
        # latest tick per symbol — overwritten by each new tick from the SDK thread
        self._latest: dict[str, dict] = {}
        self._tick_event: Optional[asyncio.Event] = None
        self._last_write_ms: dict[str, int] = {}
        self._fyers = None
        self._consumer_task: Optional[asyncio.Task] = None
        self._supervisor_task: Optional[asyncio.Task] = None
        self._stopped = False

    async def start(self) -> None:
        """Start the consumer and the SDK connection (with auto-reconnect)."""
        self._tick_event = asyncio.Event()
        self._consumer_task = self._loop.create_task(self._consume())
        self._supervisor_task = self._loop.create_task(self._supervise())
        logger.info(f"FyersTickFeed starting for {self._symbols}")

    async def stop(self) -> None:
        """Cancel tasks and tear down the SDK connection."""
        self._stopped = True
        for task in (self._consumer_task, self._supervisor_task):
            if task and not task.done():
                task.cancel()
        if self._fyers is not None:
            try:
                self._fyers.close_connection()
            except Exception:
                pass
        logger.info("FyersTickFeed stopped")

    # ── SDK lifecycle ────────────────────────────────────────────────────────

    async def _supervise(self) -> None:
        """Keep the SDK connection alive across restarts with capped backoff."""
        backoff_s = _BACKOFF_INITIAL_S
        while not self._stopped:
            try:
                await asyncio.to_thread(self._connect_blocking)
                # If we get here, connect() returned (SDK sleeps 2s and exits).
                # The actual WS lives in a daemon thread inside the SDK; we
                # only need to keep the supervisor alive so we can detect
                # disconnects via on_close.
                backoff_s = _BACKOFF_INITIAL_S
                # Idle wait — the SDK thread runs in the background. We sleep
                # and rely on the SDK's internal reconnect logic for now.
                while not self._stopped:
                    await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(
                    f"FyersTickFeed supervisor caught error: {e!r} "
                    f"— retrying in {backoff_s:.1f}s"
                )
                await asyncio.sleep(backoff_s)
                backoff_s = min(backoff_s * 2, _BACKOFF_MAX_S)

    def _connect_blocking(self) -> None:
        """Build the SDK and call connect() — runs in a worker thread.

        The SDK's connect() is sync and blocks for ~2s before returning. The
        actual WS lives in a daemon thread spawned by the SDK.
        """
        from fyers_apiv3.FyersWebsocket import data_ws

        token = get_valid_token()
        full_token = f"{settings.fyers_client_id}:{token}"

        self._fyers = data_ws.FyersDataSocket(
            access_token=full_token,
            log_path="/tmp",
            litemode=False,
            write_to_file=False,
            reconnect=True,
            on_connect=self._on_open,
            on_close=self._on_close,
            on_error=self._on_error,
            on_message=self._on_message,
        )
        self._fyers.connect()

    # ── SDK callbacks (run on SDK thread) ────────────────────────────────────

    def _on_open(self) -> None:
        logger.info(f"Fyers WS connected, subscribing to {self._symbols}")
        try:
            if self._fyers is not None:
                self._fyers.subscribe(symbols=self._symbols, data_type="SymbolUpdate")
        except Exception:
            logger.exception("Fyers WS subscribe failed")

    def _on_close(self, *args) -> None:
        logger.info(f"Fyers WS closed: {args}")

    def _on_error(self, err) -> None:
        logger.warning(f"Fyers WS error: {err!r}")

    def _on_message(self, msg) -> None:
        """Called from the SDK's WS thread for every frame.

        Only 'if' (index feed) and 'sf' (symbol feed) carry ltp+symbol. The
        control messages (cn/ful/sub) are protocol acks — ignore them.
        """
        try:
            if not isinstance(msg, dict):
                return
            msg_type = msg.get("type")
            if msg_type not in ("if", "sf"):
                return
            symbol = msg.get("symbol")
            ltp = msg.get("ltp")
            if not symbol or ltp is None:
                return
            # Overwrite the latest tick for this symbol — older intermediate
            # ticks are intentionally dropped; we only care about the most
            # recent price when the consumer next wakes up.
            self._latest[symbol] = {
                "ltp": float(ltp),
                "exch_ts": msg.get("exch_feed_time"),
            }
            # Signal the consumer (thread-safe; the Event was created on the
            # event loop).
            if self._tick_event is not None:
                self._loop.call_soon_threadsafe(self._tick_event.set)
        except Exception:
            logger.exception("Fyers WS on_message handler error")

    # ── Async consumer ───────────────────────────────────────────────────────

    async def _consume(self) -> None:
        """Drain the latest-per-symbol dict and write to Redis with throttling."""
        assert self._tick_event is not None
        while not self._stopped:
            try:
                await self._tick_event.wait()
                self._tick_event.clear()
                # Snapshot symbols with new data. dict.pop is atomic in CPython
                # so we don't race against the SDK thread overwriting entries.
                for symbol in list(self._latest.keys()):
                    data = self._latest.pop(symbol, None)
                    if data is None:
                        continue
                    now_ms = int(time.monotonic() * 1000)
                    last_ms = self._last_write_ms.get(symbol, 0)
                    if now_ms - last_ms < _WRITE_THROTTLE_MS:
                        # Skip — throttled. The next tick will overwrite this
                        # entry; if no further tick arrives, this one is lost.
                        # That's acceptable: consumers will fall back to REST.
                        continue
                    self._last_write_ms[symbol] = now_ms
                    payload = json.dumps({
                        "ltp": data["ltp"],
                        "ts": int(time.time() * 1000),
                        "exch_ts": data.get("exch_ts"),
                    })
                    try:
                        await self._redis.setex(
                            f"ltp:{symbol}", _LTP_REDIS_TTL_S, payload
                        )
                    except Exception:
                        logger.exception(f"Failed to write ltp:{symbol} to Redis")
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("FyersTickFeed consumer error — continuing")
                # Avoid a tight error loop if something is fundamentally broken.
                await asyncio.sleep(1)
