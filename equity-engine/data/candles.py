"""
Candle + quote access.

The whole pipeline depends on the ``CandleProvider`` Protocol, never on Fyers
directly. The production implementation (``CoreEngineProvider``) calls core-engine's
``/fyers/history`` and ``/fyers/quote`` endpoints over HTTP — so Fyers auth/SDK live
in exactly one place and equity-engine stays an independently-deployable consumer.

Daily bars only change once per trading day, so they are cached on disk keyed by IST
date: the full-universe EOD scan fetches each symbol at most once per day. A small
inter-request delay keeps us under Fyers' REST rate limits.

Tests inject a synthetic provider implementing the same Protocol — see
``screener.screen.__main__`` and the backtest harness.
"""

import json
import logging
import os
import time
from datetime import datetime
from typing import Optional, Protocol

import httpx
import pytz

from config import settings
from models import Bar

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")


class CandleProvider(Protocol):
    """Anything that can supply bars + a quote for a symbol."""

    def daily_bars(self, symbol: str, limit: int = 250) -> list[Bar]: ...

    def quote(self, symbol: str) -> Optional[dict]: ...


class CoreEngineProvider:
    """Fetches candles/quotes from core-engine over HTTP, with a daily disk cache."""

    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or settings.core_engine_url).rstrip("/")
        self._client = httpx.Client(timeout=settings.fetch_timeout_s)

    # ── public API ──────────────────────────────────────────────────────────
    def daily_bars(self, symbol: str, limit: int = 250) -> list[Bar]:
        cached = self._read_cache(symbol)
        if cached is not None:
            return cached[-limit:]

        bars = self._fetch_history(symbol, interval="1d", limit=limit)
        if bars:
            self._write_cache(symbol, bars)
        if settings.fetch_delay_ms:
            time.sleep(settings.fetch_delay_ms / 1000.0)
        return bars

    def quote(self, symbol: str) -> Optional[dict]:
        try:
            r = self._client.get(f"{self.base_url}/fyers/quote/{symbol}")
            r.raise_for_status()
            return r.json().get("quote")
        except (httpx.HTTPError, ValueError) as e:
            logger.warning("quote(%s) failed: %s", symbol, e)
            return None

    # ── HTTP ──────────────────────────────────────────────────────────────--
    def _fetch_history(self, symbol: str, interval: str, limit: int) -> list[Bar]:
        try:
            r = self._client.get(
                f"{self.base_url}/fyers/history/{symbol}",
                params={"interval": interval, "limit": limit},
            )
            r.raise_for_status()
            rows = r.json().get("candles", [])
            return [Bar(**row) for row in rows]
        except httpx.HTTPError as e:
            logger.warning("history(%s) failed: %s", symbol, e)
            return []

    # ── daily disk cache ──────────────────────────────────────────────────--
    def _cache_path(self, symbol: str) -> str:
        day = datetime.now(IST).strftime("%Y%m%d")
        safe = symbol.replace(":", "_")
        return os.path.join(settings.candle_cache_dir, day, f"{safe}.json")

    def _read_cache(self, symbol: str) -> Optional[list[Bar]]:
        path = self._cache_path(symbol)
        if not os.path.exists(path):
            return None
        try:
            with open(path) as f:
                rows = json.load(f)
            return [Bar(**row) for row in rows]
        except (json.JSONDecodeError, OSError, ValueError):
            return None

    def _write_cache(self, symbol: str, bars: list[Bar]) -> None:
        path = self._cache_path(symbol)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump([b.model_dump(mode="json") for b in bars], f)
        os.replace(tmp, path)


_provider: Optional[CandleProvider] = None


def get_provider() -> CandleProvider:
    """Process-wide default provider (CoreEngineProvider)."""
    global _provider
    if _provider is None:
        _provider = CoreEngineProvider()
    return _provider
