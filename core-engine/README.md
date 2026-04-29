# core-engine

The brain of the system. Ingests live market data from Fyers, computes technical indicators, queries an LLM to produce trading decisions, and publishes them to the Redis stream for the simulation engine to act on.

**Port:** `8001`

## Responsibilities

- Fyers OAuth token management (daily manual auth via `/fyers/auth`)
- 1m candle polling + in-process 5m aggregation
- Indicator computation (RSI, MACD, EMA, VWAP, CPR, S/R levels)
- LLM decision pipeline (Layer 1 daily context → Layer 2 intraday → Layer 3 forming bar)
- Entry gate enforcement (ORB, cross-symbol, proximity)
- Publishing `trading_signals` to Redis stream

## Key environment variables

| Variable | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | `ollama` | `claude` or `ollama` |
| `CANDLE_INTERVAL` | `1m` | `1m` (fetch 1m, aggregate to 5m) or `5m` (fetch 5m directly) |
| `MIN_BAR_POSITION` | `2` | Minutes into the current 5m bar before LLM runs (0–4). Set to `4` with `CANDLE_INTERVAL=5m` to replicate pre-1m behaviour. |
| `SCAN_INTERVAL_SECONDS` | `60` | How often to run the full market scan |
| `SESSION_CLOSE_HOUR` / `SESSION_CLOSE_MINUTE` | `15:15` | No new positions after this time (IST) |

See [`EXAMPLE.env`](../EXAMPLE.env) for the full variable list.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for component map, data flow, and LLM prompt structure.
