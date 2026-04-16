"""
Async HTTP client for the data-service.
Fire-and-forget writes (errors are logged but never crash the core engine).
"""

import logging
from typing import Any, Dict, List, Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import settings

logger = logging.getLogger(__name__)

_client: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=settings.data_service_url,
            timeout=5.0,
        )
    return _client


def _get_batch_client() -> httpx.AsyncClient:
    """Return a short-lived client with a generous timeout for large batch writes."""
    return httpx.AsyncClient(
        base_url=settings.data_service_url,
        timeout=60.0,
    )


async def close_client() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()


@retry(
    retry=retry_if_exception_type(httpx.TransportError),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, max=4),
    reraise=False,
)
async def _post(path: str, payload: Any) -> bool:
    try:
        resp = await get_client().post(path, json=payload)
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"data-service POST {path} failed: {e}")
        return False


async def persist_candle(candle: Dict[str, Any]) -> None:
    await _post("/api/v1/ingest/candle", candle)


async def persist_candles_batch(candles: List[Dict[str, Any]]) -> None:
    # Bootstrap batches can be 10k+ rows; use a dedicated client with 60s timeout.
    async with _get_batch_client() as client:
        try:
            resp = await client.post("/api/v1/ingest/candles", json=candles)
            resp.raise_for_status()
        except Exception as e:
            logger.warning(f"data-service POST /api/v1/ingest/candles (batch={len(candles)}) failed: {e}")


async def persist_daily_indicator(ind: Dict[str, Any]) -> None:
    await _post("/api/v1/ingest/daily-indicator", ind)


async def persist_decision(decision: Dict[str, Any]) -> None:
    await _post("/api/v1/ingest/decision", decision)


async def persist_news_batch(items: List[Dict[str, Any]]) -> None:
    if items:
        await _post("/api/v1/ingest/news", items)


async def persist_daily_ohlcv_batch(bars: List[Dict[str, Any]]) -> None:
    """Upsert a batch of daily OHLCV bars into the permanent daily_ohlcv table."""
    if not bars:
        return
    async with _get_batch_client() as client:
        try:
            resp = await client.post("/api/v1/ingest/daily-ohlcv", json=bars)
            resp.raise_for_status()
        except Exception as e:
            logger.warning(f"data-service POST /api/v1/ingest/daily-ohlcv (batch={len(bars)}) failed: {e}")


async def persist_options_oi_batch(rows: List[Dict[str, Any]]) -> None:
    """Persist a batch of options OI snapshot rows to TimescaleDB."""
    if not rows:
        return
    async with _get_batch_client() as client:
        try:
            resp = await client.post("/api/v1/ingest/options-oi", json=rows)
            resp.raise_for_status()
        except Exception as e:
            logger.warning(f"data-service POST /api/v1/ingest/options-oi (batch={len(rows)}) failed: {e}")


async def persist_sr_levels(symbol: str, levels: List[Dict[str, Any]]) -> None:
    """Replace the computed S/R level set for a symbol."""
    await _post("/api/v1/ingest/sr-levels", {"symbol": symbol, "levels": levels})


async def fetch_sr_levels(
    symbol: str,
    near_price: Optional[float] = None,
    limit: int = 25,
) -> List[Dict[str, Any]]:
    """Fetch historical S/R levels from data-service, optionally filtered by price proximity."""
    try:
        params: Dict[str, Any] = {"symbol": symbol, "limit": limit}
        if near_price:
            params["near_price"] = near_price
        resp = await get_client().get("/api/v1/sr-levels", params=params, timeout=5.0)
        resp.raise_for_status()
        return resp.json().get("levels", [])
    except Exception as e:
        logger.warning(f"Could not fetch SR levels for {symbol}: {e}")
        return []


async def fetch_magnet_zones(symbol: str) -> Optional[Dict[str, Any]]:
    """Fetch unfilled gap and unbreached CPR magnet zones for a symbol from data-service."""
    try:
        import urllib.parse
        encoded = urllib.parse.quote(symbol, safe="")
        resp = await get_client().get(
            f"/api/v1/magnets/{encoded}",
            timeout=8.0,
        )
        resp.raise_for_status()
        data = resp.json()
        return {"gaps": data.get("gaps", []), "cprs": data.get("cprs", [])}
    except Exception as e:
        logger.warning(f"Could not fetch magnet zones for {symbol}: {e}")
        return None


async def fetch_daily_candles(symbol: str, limit: int = 14) -> List[Dict[str, Any]]:
    """Fetch last N daily candles for a symbol from data-service."""
    try:
        resp = await get_client().get(
            "/api/v1/historical-data",
            params={"symbol": symbol, "interval": "daily", "limit": limit},
            timeout=5.0,
        )
        resp.raise_for_status()
        return resp.json().get("candles", [])
    except Exception as e:
        logger.warning(f"Could not fetch daily candles for {symbol}: {e}")
        return []


async def update_volume_profile(symbol: str, session_date: str) -> None:
    """Trigger an incremental volume profile update for one session date."""
    await _post("/api/v1/volume-profile/update", {"symbol": symbol, "session_date": session_date})


async def fetch_volume_profile(symbol: str) -> List[Dict[str, Any]]:
    """Fetch historical average 5m volume per time slot for a symbol from data-service."""
    try:
        import urllib.parse
        encoded = urllib.parse.quote(symbol, safe="")
        resp = await get_client().get(
            f"/api/v1/volume-profile/{encoded}",
            timeout=8.0,
        )
        resp.raise_for_status()
        return resp.json().get("slots", [])
    except Exception as e:
        logger.warning(f"Could not fetch volume profile for {symbol}: {e}")
        return []


async def fetch_context_snapshot(symbol: str) -> Optional[Dict[str, Any]]:
    """Fetch the latest context snapshot for a symbol from data-service."""
    try:
        resp = await get_client().get(
            "/api/v1/context-snapshot",
            params={"symbol": symbol},
            timeout=8.0,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("context")
    except Exception as e:
        logger.warning(f"Could not fetch context snapshot for {symbol}: {e}")
        return None
