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
    await _post("/api/v1/ingest/candles", candles)


async def persist_daily_indicator(ind: Dict[str, Any]) -> None:
    await _post("/api/v1/ingest/daily-indicator", ind)


async def persist_decision(decision: Dict[str, Any]) -> None:
    await _post("/api/v1/ingest/decision", decision)


async def persist_news_batch(items: List[Dict[str, Any]]) -> None:
    if items:
        await _post("/api/v1/ingest/news", items)


async def persist_daily_ohlcv_batch(bars: List[Dict[str, Any]]) -> None:
    """Upsert a batch of daily OHLCV bars into the permanent daily_ohlcv table."""
    if bars:
        await _post("/api/v1/ingest/daily-ohlcv", bars)


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
