"""
Unit tests for select_candles_to_persist in core-engine/scheduler/jobs.py.

Regression coverage for the stale-1m-bar bug discovered 2026-05-15:
the scheduler used `c.timestamp > last_ts` so once a partial bar at
last_ts was persisted, the same minute was never re-selected — Fyers'
later-finalised view was silently dropped. The fix is `>=`: the bar at
last_ts gets one more upsert when the next scan pulls its finalised
values from Fyers. upsert_candle is idempotent so re-writing an already-
finalised bar costs nothing.
"""
from __future__ import annotations

from datetime import datetime, timezone

from models.schemas import OHLCBar
from scheduler.candle_filter import select_candles_to_persist


def _bar(t: datetime, c: float = 100.0) -> OHLCBar:
    """Minimal OHLCBar at time t (close=c, others irrelevant for filter)."""
    return OHLCBar(timestamp=t, open=c, high=c, low=c, close=c, volume=0)


UTC = timezone.utc


# ── Regression: the partial bar at last_ts must be re-included ────────────────

class TestSelectCandlesIncludesLastTs:
    """The bar whose timestamp == last_ts must be in the result so its
    finalised OHLCV gets upserted on the next scan."""

    def test_bar_at_last_ts_is_included(self):
        last_ts = datetime(2026, 5, 15, 8, 17, tzinfo=UTC)  # 13:47 IST
        candles = [
            _bar(datetime(2026, 5, 15, 8, 16, tzinfo=UTC)),   # 13:46 — before
            _bar(datetime(2026, 5, 15, 8, 17, tzinfo=UTC)),   # 13:47 — == last_ts
            _bar(datetime(2026, 5, 15, 8, 18, tzinfo=UTC)),   # 13:48 — after
        ]
        result = select_candles_to_persist(candles, last_ts)
        assert [c.timestamp.minute for c in result] == [17, 18]

    def test_only_bar_at_last_ts_still_included(self):
        """When no newer bar exists yet, the bar at last_ts alone must still
        get re-upserted — that's exactly the moment its finalised values
        arrive after the minute closes."""
        last_ts = datetime(2026, 5, 15, 8, 17, tzinfo=UTC)
        candles = [
            _bar(datetime(2026, 5, 15, 8, 16, tzinfo=UTC)),
            _bar(datetime(2026, 5, 15, 8, 17, tzinfo=UTC)),
        ]
        result = select_candles_to_persist(candles, last_ts)
        assert len(result) == 1
        assert result[0].timestamp.minute == 17


# ── Older bars stay filtered out ──────────────────────────────────────────────

class TestSelectCandlesExcludesOlder:
    """We don't want to re-rewrite every bar in the 500-bar fetched window
    every scan — only bars from last_ts onward."""

    def test_older_bars_excluded(self):
        last_ts = datetime(2026, 5, 15, 8, 17, tzinfo=UTC)
        candles = [
            _bar(datetime(2026, 5, 15, 8, 10, tzinfo=UTC)),
            _bar(datetime(2026, 5, 15, 8, 11, tzinfo=UTC)),
            _bar(datetime(2026, 5, 15, 8, 17, tzinfo=UTC)),
            _bar(datetime(2026, 5, 15, 8, 18, tzinfo=UTC)),
        ]
        result = select_candles_to_persist(candles, last_ts)
        assert [c.timestamp.minute for c in result] == [17, 18]


# ── Timezone handling ────────────────────────────────────────────────────────

class TestSelectCandlesTimezone:
    """Bar timestamps may arrive in IST (Asia/Kolkata, UTC+5:30) but last_ts
    is stored in UTC. The helper must normalise both sides to UTC."""

    def test_ist_bar_timestamp_matches_utc_last_ts(self):
        # 2026-05-15 13:47 IST == 08:17 UTC
        import pytz
        IST = pytz.timezone("Asia/Kolkata")
        last_ts_utc = datetime(2026, 5, 15, 8, 17, tzinfo=UTC)
        ist_bar = OHLCBar(
            timestamp=IST.localize(datetime(2026, 5, 15, 13, 47)),
            open=100, high=100, low=100, close=100, volume=0,
        )
        result = select_candles_to_persist([ist_bar], last_ts_utc)
        assert len(result) == 1


# ── Empty input ──────────────────────────────────────────────────────────────

class TestSelectCandlesEdges:

    def test_empty_input(self):
        last_ts = datetime(2026, 5, 15, 8, 17, tzinfo=UTC)
        assert select_candles_to_persist([], last_ts) == []

    def test_all_older_returns_empty(self):
        last_ts = datetime(2026, 5, 15, 8, 17, tzinfo=UTC)
        candles = [
            _bar(datetime(2026, 5, 15, 8, 15, tzinfo=UTC)),
            _bar(datetime(2026, 5, 15, 8, 16, tzinfo=UTC)),
        ]
        assert select_candles_to_persist(candles, last_ts) == []
