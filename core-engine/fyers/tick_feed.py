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


# Tunables — tuned 2026-05-16 after empirical sampling.
_LTP_REDIS_TTL_S = 30
# 200ms — observed tick interval p50 ≈ 388ms (NIFTY50), 467ms (NIFTYBANK) on
# 2026-05-15. 500ms had been dropping ~half of writes with up to 17.65pt
# drift between kept writes on BANKNIFTY — enough to miss an intra-second
# cross of an EMA/CPR invalidation level. 200ms tracks ticks ~1:1 during
# active trading at a trivial Redis cost (~5 writes/s/symbol).
_WRITE_THROTTLE_MS = 200
# forming_bar:{symbol} — should always be refreshed within a minute by the
# next tick, but TTL 90s lets the chart show "stale forming bar" briefly
# rather than going blank during a brief pause in ticks.
_FORMING_BAR_TTL_S = 90
# last_bar:{symbol} — held for 120s after a minute rolls over so the chart
# has continuity in the 60s gap before the next REST history pull catches up.
_LAST_BAR_TTL_S = 120
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
        # in-progress 1m bar per symbol, accumulated from ticks (open=first tick
        # of minute, high/low=running extremes, close=latest). Mutated from the
        # SDK thread; consumer reads + writes to Redis. dict ops are atomic in
        # CPython so no lock is needed.
        self._forming_bars: dict[str, dict] = {}
        # Stash just-finalised bars when the minute rolls over so the consumer
        # can persist them with a short TTL (last_bar:{symbol}, 120s) — gives
        # the chart a fallback for the 60s window between minute close and
        # the next Fyers REST history pull catching up.
        self._finalized_bars: dict[str, dict] = {}
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
            ltp_raw = msg.get("ltp")
            if not symbol or ltp_raw is None:
                return
            ltp = float(ltp_raw)
            exch_ts = msg.get("exch_feed_time")

            # Overwrite the latest tick for this symbol — older intermediate
            # ticks are intentionally dropped; we only care about the most
            # recent price when the consumer next wakes up.
            self._latest[symbol] = {"ltp": ltp, "exch_ts": exch_ts}

            # Maintain an in-progress 1m bar from ticks. Minute boundary is
            # derived from exch_feed_time (epoch seconds) so the bar lines
            # up with Fyers' authoritative bars rather than our wall clock.
            if exch_ts:
                bar_min = (int(exch_ts) // 60) * 60
                fb = self._forming_bars.get(symbol)
                if fb is None or fb["bar_min"] != bar_min:
                    # Minute rollover (or first tick for this symbol). Hand the
                    # previous (now-finalised) bar to the consumer for a short-
                    # TTL stash, then start fresh on this tick.
                    if fb is not None:
                        self._finalized_bars[symbol] = fb
                    self._forming_bars[symbol] = {
                        "bar_min": bar_min,
                        "open":  ltp, "high": ltp, "low": ltp, "close": ltp,
                        "n":     1,
                    }
                else:
                    if ltp > fb["high"]: fb["high"] = ltp
                    if ltp < fb["low"]:  fb["low"]  = ltp
                    fb["close"] = ltp
                    fb["n"] += 1

            # Signal the consumer (thread-safe; the Event was created on the
            # event loop).
            if self._tick_event is not None:
                self._loop.call_soon_threadsafe(self._tick_event.set)
        except Exception:
            logger.exception("Fyers WS on_message handler error")

    # ── Async consumer ───────────────────────────────────────────────────────

    async def _consume(self) -> None:
        """Drain the latest-per-symbol dict and write to Redis with throttling.

        Also persists the in-progress 1m bar to forming_bar:{symbol} (subject
        to the same throttle) so the chart can show ticks moving the current
        candle. Any bar that just finalised (minute rollover) is written to
        last_bar:{symbol} with a 120s TTL so the chart has continuity across
        the 60s window between minute close and the next Fyers REST history
        pull.
        """
        assert self._tick_event is not None
        while not self._stopped:
            try:
                await self._tick_event.wait()
                self._tick_event.clear()

                # ── Finalised bars (minute rollover) → last_bar:* ─────────
                # Process these before forming bars so a brief race never
                # ends with a stale forming bar shadowing a fresher last bar.
                for symbol in list(self._finalized_bars.keys()):
                    fb = self._finalized_bars.pop(symbol, None)
                    if fb is None:
                        continue
                    payload = json.dumps(_bar_to_payload(fb))
                    try:
                        await self._redis.setex(
                            f"last_bar:{symbol}", _LAST_BAR_TTL_S, payload
                        )
                    except Exception:
                        logger.exception(f"Failed to write last_bar:{symbol}")

                # ── Latest tick + in-progress bar → ltp:* and forming_bar:* ─
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

                    # Also persist the forming bar at the same cadence. Read
                    # the current value just-in-time (SDK may have updated it
                    # between throttle check and now — that's desirable; we
                    # publish the freshest available).
                    fb = self._forming_bars.get(symbol)
                    if fb is not None:
                        fb_payload = json.dumps(_bar_to_payload(fb))
                        try:
                            await self._redis.setex(
                                f"forming_bar:{symbol}", _FORMING_BAR_TTL_S, fb_payload
                            )
                        except Exception:
                            logger.exception(
                                f"Failed to write forming_bar:{symbol}"
                            )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("FyersTickFeed consumer error — continuing")
                # Avoid a tight error loop if something is fundamentally broken.
                await asyncio.sleep(1)


def _bar_to_payload(fb: dict) -> dict:
    """Convert an internal forming-bar dict to the wire shape consumers expect.
    Time is ISO8601 UTC at the minute boundary (matches /historical-data)."""
    from datetime import datetime, timezone
    t = datetime.fromtimestamp(fb["bar_min"], tz=timezone.utc).isoformat()
    return {
        "time":   t,
        "open":   fb["open"],
        "high":   fb["high"],
        "low":    fb["low"],
        "close":  fb["close"],
        "volume": 0,            # indices: no volume in the feed
        "n_ticks": fb.get("n", 0),
        "ts":     int(time.time() * 1000),
    }
