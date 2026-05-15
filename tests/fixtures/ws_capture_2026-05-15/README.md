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
