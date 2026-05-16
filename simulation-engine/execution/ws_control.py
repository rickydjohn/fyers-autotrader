"""
HTTP client for core-engine's WS subscription endpoints.

When a position opens, simulation-engine tells core-engine to attach the
Fyers WebSocket to the option symbol so its LTP starts flowing into Redis
sub-second (`ltp:{option_symbol}`). When the position closes, we detach.

Both calls are best-effort: failures are logged but never raised. The
core-engine's periodic reconcile (every 5 minutes) picks up any missed
sub/unsub by walking `positions:open` itself, so a momentary HTTP error
costs us at most ~5 minutes of REST-fallback freshness on that one option.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)

# Short timeout: this call is in the hot path of opening/closing a position.
# If core-engine is unreachable we'd rather give up fast than block the trade.
_TIMEOUT_S = 2.0


async def subscribe(symbol: Optional[str]) -> None:
    """Tell core-engine to subscribe the Fyers WS to `symbol`."""
    if not symbol:
        return
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            r = await client.post(
                f"{settings.core_engine_url}/ws/subscribe",
                params={"symbol": symbol},
            )
            if r.status_code != 200:
                logger.warning(
                    f"WS subscribe non-200 for {symbol}: HTTP {r.status_code}"
                )
    except Exception as e:
        # Don't propagate — periodic reconcile in core-engine will catch up.
        logger.warning(f"WS subscribe failed for {symbol}: {e}")


async def unsubscribe(symbol: Optional[str]) -> None:
    """Tell core-engine to detach the Fyers WS from `symbol`."""
    if not symbol:
        return
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            r = await client.post(
                f"{settings.core_engine_url}/ws/unsubscribe",
                params={"symbol": symbol},
            )
            if r.status_code != 200:
                logger.warning(
                    f"WS unsubscribe non-200 for {symbol}: HTTP {r.status_code}"
                )
    except Exception as e:
        logger.warning(f"WS unsubscribe failed for {symbol}: {e}")
