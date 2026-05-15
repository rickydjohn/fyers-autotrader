"""
Helpers for choosing which 1m candles to upsert into market_candles.

Lives in its own module (no heavy deps) so it can be unit-tested without
pulling in the full scheduler graph.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List

from models.schemas import OHLCBar


def select_candles_to_persist(
    candles: List[OHLCBar],
    last_ts: datetime,
) -> List[OHLCBar]:
    """Return the slice of `candles` whose timestamp is >= `last_ts`.

    Uses `>=` (not `>`) so a bar that was previously persisted while partial
    can be re-upserted once Fyers finalises it. Without this, the bar at
    last_ts is filtered out forever and the DB keeps its partial state.

    Empirically confirmed 2026-05-15 (see
    tests/fixtures/ws_capture_2026-05-15/): with strict `>`, consecutive 1m
    bars showed 10-20pt phantom gaps because each was frozen at the first
    partial snapshot taken mid-minute. With `>=`, the same bar gets one more
    upsert after the minute closes; upsert_candle is idempotent (OHLCV
    overwrites; indicator columns COALESCE), so re-writing an already-final
    bar is cheap and safe.

    `last_ts` is expected to be tz-aware (UTC). Bar timestamps are converted
    to UTC for the comparison.
    """
    return [c for c in candles if c.timestamp.astimezone(timezone.utc) >= last_ts]
