"""
Lightweight async HTTP client for data-service writes from simulation-engine.
Errors are non-fatal — Redis remains the operational source of truth.
"""

import logging
from typing import Any, Dict, Optional

import httpx

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


async def persist_trade(trade: Dict[str, Any]) -> None:
    try:
        resp = await get_client().post("/api/v1/ingest/trade", json=trade)
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"data-service trade persist failed: {e}")


async def mark_decision_acted(decision_id: str, trade_id: str) -> None:
    try:
        resp = await get_client().patch(
            f"/api/v1/ingest/decision/{decision_id}/acted",
            params={"trade_id": trade_id},
        )
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"data-service acted_upon update failed: {e}")
