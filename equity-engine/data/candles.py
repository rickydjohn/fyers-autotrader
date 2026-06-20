"""
Candle + quote access — TimescaleDB read-through.

Daily bars are persisted in TimescaleDB (data-service `daily_ohlcv`). Reads go to the
DB first; Fyers (via core-engine) is hit ONLY to populate missing history or append
the latest day, and the result is upserted back. So a backtest or repeated scan reads
years of bars instantly from the DB instead of re-fetching thousands of symbols from
Fyers every run.

Everything downstream depends only on the ``CandleProvider`` Protocol, so tests inject
synthetic bars.
"""

import logging
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
    def daily_bars(self, symbol: str, limit: int = 250) -> list[Bar]: ...
    def quote(self, symbol: str) -> Optional[dict]: ...


def _merge_bars(a: list[Bar], b: list[Bar]) -> list[Bar]:
    """Merge two bar lists by calendar date (b overrides a), oldest-first."""
    by_date = {bar.timestamp.date(): bar for bar in a}
    for bar in b:
        by_date[bar.timestamp.date()] = bar
    return [by_date[d] for d in sorted(by_date)]


class CoreEngineProvider:
    """Daily bars from TimescaleDB (read-through), quotes from core-engine."""

    def __init__(self, core_url: Optional[str] = None, data_url: Optional[str] = None):
        self.core_url = (core_url or settings.core_engine_url).rstrip("/")
        self.data_url = (data_url or settings.data_service_url).rstrip("/")
        self._client = httpx.Client(timeout=settings.fetch_timeout_s)

    # ── public API ──────────────────────────────────────────────────────────
    def daily_bars(self, symbol: str, limit: int = 250) -> list[Bar]:
        db = self._db_get(symbol, limit + 5)
        today = datetime.now(IST).date()
        have_today = bool(db) and db[-1].timestamp.date() >= today
        enough = len(db) >= limit
        if enough and have_today:
            return db[-limit:]

        # DB short (need deep history) → full fetch; else a small recent top-up.
        fetch_n = limit if not enough else 40
        fresh = self._fetch_history(symbol, "1d", fetch_n)
        if fresh:
            self._db_upsert(symbol, fresh)
            db = _merge_bars(db, fresh)
            if settings.fetch_delay_ms:
                time.sleep(settings.fetch_delay_ms / 1000.0)
        return (db or fresh)[-limit:]

    def quote(self, symbol: str) -> Optional[dict]:
        try:
            r = self._client.get(f"{self.core_url}/fyers/quote/{symbol}")
            r.raise_for_status()
            return r.json().get("quote")
        except (httpx.HTTPError, ValueError) as e:
            logger.warning("quote(%s) failed: %s", symbol, e)
            return None

    # ── TimescaleDB (data-service) ────────────────────────────────────────────
    def _db_get(self, symbol: str, limit: int) -> list[Bar]:
        try:
            r = self._client.get(f"{self.data_url}/api/v1/daily-ohlcv",
                                 params={"symbol": symbol, "limit": limit})
            r.raise_for_status()
            rows = r.json().get("bars", [])
            return [
                Bar(
                    timestamp=IST.localize(datetime.fromisoformat(str(b["date"])[:10])),
                    open=float(b["open"]), high=float(b["high"]), low=float(b["low"]),
                    close=float(b["close"]), volume=int(b["volume"] or 0),
                )
                for b in rows
            ]
        except (httpx.HTTPError, ValueError, KeyError) as e:
            logger.warning("db_get(%s) failed: %s", symbol, e)
            return []

    def _db_upsert(self, symbol: str, bars: list[Bar]) -> None:
        payload = [
            {
                "date": b.timestamp.strftime("%Y-%m-%d"), "symbol": symbol,
                "open": b.open, "high": b.high, "low": b.low, "close": b.close,
                "volume": int(b.volume or 0),
            }
            for b in bars
        ]
        try:
            r = self._client.post(f"{self.data_url}/api/v1/ingest/daily-ohlcv", json=payload)
            r.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("db_upsert(%s) failed: %s", symbol, e)

    # ── Fyers (via core-engine) ───────────────────────────────────────────────
    def _fetch_history(self, symbol: str, interval: str, limit: int) -> list[Bar]:
        try:
            r = self._client.get(f"{self.core_url}/fyers/history/{symbol}",
                                 params={"interval": interval, "limit": limit})
            r.raise_for_status()
            rows = r.json().get("candles", [])
            return [Bar(**row) for row in rows]
        except httpx.HTTPError as e:
            logger.warning("history(%s) failed: %s", symbol, e)
            return []


_provider: Optional[CandleProvider] = None


def get_provider() -> CandleProvider:
    global _provider
    if _provider is None:
        _provider = CoreEngineProvider()
    return _provider
