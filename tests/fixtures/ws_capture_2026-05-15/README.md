# WS forming-bar fixture pack — 2026-05-15 IST

Captured during live trading (~13:46–13:52 IST) so the forming-bar / tick-stream work can be developed and tested offline.

## Files

| File | What it is |
|---|---|
| `tick_capture.jsonl` | 5 minutes of raw Fyers WS messages (1257 `if` ticks + control frames). One JSON per line; each tick has `_recv_ts` added by the capture script. Covers 5 full minute-boundary transitions per symbol (4 boundaries × 2 symbols = ~8 visible rollovers — useful for testing forming-bar minute-rollover logic). |
| `tick_capture_script.py` | The capture script that produced `tick_capture.jsonl`. Re-runnable any time during market hours; configurable via `CAPTURE_SECONDS`, `CAPTURE_OUT`, `CAPTURE_SYMBOLS` env. |
| `historical-data_nifty50_1m.json` | `GET /api/v1/historical-data?symbol=NSE:NIFTY50-INDEX&interval=1m&limit=15` response — the exact shape the chart's `fetchHistoricalData` consumes. |
| `historical-data_niftybank_1m.json` | Same for BANKNIFTY. |
| `market-data_nifty50.json` | `GET /api/v1/market-data?symbol=NSE:NIFTY50-INDEX` — the scan-time snapshot blob (ltp, change, indicators, 50 candles, news). |
| `market-data_niftybank.json` | Same for BANKNIFTY. |
| `db_market_candles_today.json` | First 30 rows of `market_candles` for both symbols from today, newest-first. Authoritative 1m bars from Fyers REST history — what we'd compare WS-synthesized bars against. |

## Observed tick characteristics

| Metric | NIFTY50 | NIFTYBANK |
|---|---|---|
| Rate over the 5-min window | 2.38 / s | 1.81 / s |
| Inter-tick median (ms) | 388 | 467 |
| Inter-tick p95 (ms) | 526 | 869 |
| Inter-tick p99 (ms) | 845 | 938 |

Tick message keys: `ltp, prev_close_price, ch, chp, exch_feed_time, high_price, low_price, open_price, type, symbol` — note that `open_price` / `high_price` / `low_price` are **day-level**, not minute-level, so forming-bar synthesis must accumulate its own per-minute OHLC from `ltp` and `_recv_ts`.

## When working on forming-bar / tick-driven exits offline

Load `tick_capture.jsonl` directly as a replay stream. Each `_recv_ts` is wall-clock time in IST — use it to drive a simulated clock so your forming-bar logic sees the same minute-boundary transitions the live system saw on 2026-05-15.

`historical-data_*.json` shows the exact response shape the chart expects, so a future "merge forming bar into historical-data response" patch can be written without guessing the schema.

## Findings from the offline analysis (`analyze_ws_capture.py`)

### 1. DB `market_candles` has stale/partial 1m bars

Comparing WS-synthesized 1m bars against the DB row for the same minute (using `analyze_ws_capture.py`) revealed a systematic problem: DB bars are point-snapshots written mid-minute and **never refreshed after the minute closes**. Example from 2026-05-15 13:47 NIFTY50:

| | open | high | low | close |
|---|---|---|---|---|
| WS-synth (75 ticks 13:47:29–59) | 23731.20 | 23732.50 | 23714.70 | 23714.70 |
| DB (Fyers history snapshot) | 23736.75 | 23737.05 | 23733.35 | 23733.35 |

The whole-minute view of 13:47 from the DB looks like a 4-second window written at ~13:47:15 then frozen. The 13:48 bar in the DB opens at 23714.40 — exactly where the WS ends 13:47 — so **internal continuity is violated**: a 19-point "gap" appears at the 13:47→13:48 boundary that doesn't exist in reality.

Root cause is in `core-engine/scheduler/jobs.py`:
```python
new_candles = [c for c in candles_1m if c.timestamp.astimezone(_tz.utc) > last_ts]
```
The strict `>` means once a partial bar is persisted, its timestamp becomes `last_ts` and the same bar is never selected again — even after Fyers finalises it. Indices commonly show this as narrow individual bars (5-10 pts) with large gaps between consecutive bars (10-20 pts).

Fix surface area: change the filter and add a small "always re-persist the last N minutes" window to catch finalisations. Fyers' raw REST history is healthy (confirmed by hitting it directly — consecutive bars have sub-1pt continuity), so this is purely a scheduler issue.

### 2. Tick rate is throttled to roughly 2 / s, not 5–10 / s

Empirically observed (5-min window, market hours):

| Symbol | rate /s | inter-tick p50 | p95 | p99 | max gap |
|---|---|---|---|---|---|
| NIFTY50 | 2.38 | 388 ms | 526 ms | 845 ms | 1230 ms |
| NIFTYBANK | 1.81 | 467 ms | 869 ms | 938 ms | 1230 ms |

Earlier estimates of 4-10 /s were too aggressive. The actual rate is one tick every ~400-500 ms, with occasional 800-1200 ms silent stretches but never > 1.3 s.

### 3. 500 ms throttle suppresses ~half of writes, with non-trivial price drift between kept writes

Under our current `_WRITE_THROTTLE_MS = 500`:

| Symbol | ticks in 5 min | writes kept | suppressed | max Δprice between kept writes |
|---|---|---|---|---|
| NIFTY50 | 715 | 375 | 340 | 3.50 |
| NIFTYBANK | 542 | 349 | 193 | 17.65 |

For NIFTYBANK that 17.65 pt max-drift between writes is meaningful — if a tick-driven invalidation exit checks `ltp:` only when Redis gets written, it could miss a transient cross of an EMA/CPR level. Worth lowering the throttle to ~250 ms (or removing it) when tick-driven exits ship.

### 4. Typical 1m bar size (informs UI scale for forming-bar rendering)

| Symbol | bars | min range | median | max | avg |
|---|---|---|---|---|---|
| NIFTY50 | 6 | 4.60 | 9.30 | 17.80 | 9.90 |
| NIFTYBANK | 6 | 12.40 | 27.80 | 51.80 | 27.02 |

Small numbers, but enough for the chart to need visible vertical movement on a forming bar. A naïve 1-tick close-only update on the last candle would visibly wobble it.
