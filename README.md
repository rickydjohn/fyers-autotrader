# Trading Intelligence System

NSE intraday autotrader using Fyers (REST + WebSocket), TimescaleDB, and a pluggable LLM backend (Ollama gpt-oss:120b-cloud or Claude Haiku 4.5).

## Architecture

```
┌────────────────────────────────────────────────────────────┐
│                        HOST MACHINE                         │
│  Ollama proxy :11434 (local)     Browser → :3000            │
└─────────────────────┬──────────────────────────────────────┘
                      │
   ┌─────────────────┴──────────────────────────────────────┐
   │               trading-net (Docker bridge)              │
   │                                                        │
   │  core-engine:8001                                      │
   │  ├─ Fyers REST (quotes/history)   via Squid proxy      │
   │  ├─ Fyers WebSocket (ticks)       direct (Cloudflare)  │
   │  ├─ FyersTickFeed → Redis ltp:* / forming_bar:*        │
   │  ├─ Scheduler (60s scan, 5s position watcher)          │
   │  └─ LLM call (Ollama 120b cloud or Claude Haiku)       │
   │                                                        │
   │  simulation-engine:8002                                │
   │  ├─ Decision consumer (XREAD)                          │
   │  ├─ Drift veto + ORB/CPR/consolidation/proximity gates │
   │  ├─ Tick-driven invalidation exits                     │
   │  └─ Premium SL / milestone trail (exit_rules)          │
   │                                                        │
   │  api-service:8000  REST + SSE; /market-data overlays   │
   │                    WS-fresh ltp; /forming-bar @ 1Hz    │
   │                                                        │
   │  data-service:8003  SQLAlchemy → TimescaleDB           │
   │                                                        │
   │  ui-service:3000   React chart, polls /forming-bar 1Hz │
   │                                                        │
   │  TimescaleDB:5432    Redis:6379                        │
   └────────────────────────────────────────────────────────┘
```

A **single Fyers WebSocket** subscribes to all configured symbols at once (`FyersDataSocket.subscribe(symbols=[…])`); the SDK multiplexes them over one TLS connection. There is no per-symbol WS.

See [ARCHITECTURE.md](ARCHITECTURE.md) for sequence diagrams, the entry/exit pipelines, and the full Redis keyspace.

## Services

| Service | Port | Role |
|---|---|---|
| [core-engine](core-engine/README.md) | 8001 | Fyers REST + WebSocket, indicators, LLM decisions |
| [simulation-engine](simulation-engine/README.md) | 8002 | Decision consumer, entry gates, drift veto, invalidation + premium exits |
| [data-service](data-service/README.md) | 8003 | TimescaleDB ORM, historical context, S/R levels |
| [api-service](api-service/README.md) | 8000 | REST + SSE gateway, live-LTP overlay, forming-bar endpoint |
| [ui-service](ui-service/README.md) | 3000 | React dashboard, tick-consolidated chart |

## Setup

### 1. Prerequisites

- Docker + Docker Compose
- Fyers account with API credentials
- Ollama (local with cloud-hosted model) **or** Anthropic API key

### 2. LLM provider

**Option A — Ollama gpt-oss:120b-cloud (current production default)**

```bash
ollama pull gpt-oss:120b-cloud
ollama serve   # port 11434
```
```
LLM_PROVIDER=ollama
OLLAMA_MODEL=gpt-oss:120b-cloud
OLLAMA_ENDPOINT=http://host.docker.internal:11434
OLLAMA_TIMEOUT=120
```

**Option B — Claude Haiku 4.5 (faster, costs ≈ $0.33 per backtest run)**
```
LLM_PROVIDER=claude
CLAUDE_API_KEY=sk-ant-...
CLAUDE_MODEL=claude-haiku-4-5-20251001
```

### 3. Configure credentials

```
FYERS_CLIENT_ID=xxxxxxxxxxx100
FYERS_SECRET_KEY=xxxxxxxxxx
FYERS_REDIRECT_URI=http://localhost:8001/fyers/callback

INITIAL_BUDGET=100000
LLM_DECISION_INTERVAL_SECONDS=60
GREEKS_POLL_INTERVAL_SECONDS=5
CANDLE_INTERVAL=1m
MIN_BAR_POSITION=2
```

### 4. Start all services

```bash
docker compose up --build
```

### 5. Authenticate with Fyers (daily)

```
http://localhost:8001/fyers/auth
```
Token persists in a Docker volume and is reused; it expires at 6 AM IST the next day.

### 6. Open the dashboard

```
http://localhost:3000
```

## Trading Logic

A decision goes through these layers in order before a trade executes:

```
LLM → confidence ≥ 0.70 → fresh-price refresh → ORB → CPR → consolidation → proximity → broker
```

### Confidence floor
`confidence < 0.70` becomes HOLD.

### Fresh-price refresh (runs BEFORE the gates)
Right after the confidence floor, fetch a live LTP. The downstream gates evaluate against this fresh price rather than the scan-time snapshot, which can be tens of seconds (or minutes on Ollama 120b runs) stale.

### ORB gate
No trades before 09:30 IST. After that, BUY requires price > `orb_high × (1 + ORB_BUFFER)`, SELL requires the symmetric break below `orb_low`. Default buffer 0.20%.

**ORB-after-break relaxation:** once price has crossed either threshold today, the gate is disabled for the rest of the session — both directions become tradable regardless of where current price sits in the ORB. A 7-month backtest of 147 days showed ~75% of break-days produce material follow-through in some direction (clean continuation, whipsaw-then-trend, or reversal); only ~10–14% are true false-breakout mean reversions. Lazily re-checked from data-service on first signal after a sim restart.

### CPR gate
BUY requires price > max(TC, BC) × 1.002. SELL requires price < min(TC, BC) × 0.998. Handles inverted CPR (BC > TC) symmetrically.

### Consolidation gate
Block all entries when the symbol is inside a tight consolidation range and the LLM hasn't claimed a breakout.

### Entry-proximity gate
Block BUY if the nearest static resistance (PDH / CPR / pivots; **not** running day extremes) is within 0.25%. Symmetric for SELL.

### Tick-driven invalidation exits (exit-side)
At position open, the indicator levels the LLM's thesis was built on (`vwap`, `ema_21`, `cpr_tc`, `cpr_bc`) are frozen onto the Position. On every consumer tick (~5s, the underlying read from the WS-fed `ltp:` cache), the helper checks whether the underlying has crossed back through any of those levels in the direction opposite the trade. If yes, exit immediately with `INVALIDATION_<LEVEL>`. Catches "thesis broken" cases minutes before the option's −10% premium SL fires.

## Chart updates

The forming-bar pipeline (added 2026-05-16) consolidates WS ticks into a 1Hz chart update for every timeframe:

- **Backend** (core-engine): the tick feed accumulates per-minute OHLC and writes `forming_bar:{symbol}` to Redis (~200ms throttle); a just-finalised bar is written to `last_bar:{symbol}` (120s TTL).
- **API**: `GET /api/v1/market-data/forming-bar?symbol=…` returns both.
- **UI**: polls every 1s for every timeframe. On 1m it splices the forming bar as its own candle; on 5m / 15m / 1h it patches the LAST aggregated candle's close and extends its high/low. React skips the re-render when nothing changed.

The 200ms backend cadence is decoupled from the 1s UI cadence on purpose — drift veto and invalidation exits benefit from sub-second freshness, while the chart never flickers tick-by-tick.

## Backtesting

All backtests run inside the trading-data container (they use asyncpg against the live TimescaleDB):

```bash
docker cp tests/backtests/backtest_cpr_gate.py trading-data:/tmp/ && \
docker exec trading-data python /tmp/backtest_cpr_gate.py
```

Available:

| Script | What it answers |
|---|---|
| `backtest_cpr_gate.py` | How many historical trades would the CPR gate have blocked, and what was their P&L? |
| `backtest_orb_gate.py` | Same for the ORB gate (supports `ORB_BUFFER` env override for buffer sweeps) |
| `backtest_cross_symbol_gate.py` | Win-rate delta from the NIFTY → BANKNIFTY confidence gate |
| `backtest_new_rules.py` | Apply an alternate rule set to historical `ai_decisions` |
| `backtest_range_breakout.py` | Validate the intraday consolidation-breakout rule |
| `backtest_candles.py` | LLM decision replay on historical candles (runs inside trading-core) |

Refresh today's bars from Fyers history:
```bash
curl -X POST http://localhost:8001/historical/backfill
```

## API endpoints

Base URL: `http://localhost:8000/api/v1`

| Endpoint | Method | Notes |
|---|---|---|
| `/market-data?symbol=…` | GET | Snapshot + WS-fresh `ltp` overlay when available |
| `/market-data/forming-bar?symbol=…` | GET | In-progress 1m bar from ticks (1Hz UI poll) |
| `/historical-data?symbol=…&interval=…&limit=…` | GET | Historical OHLC from `market_candles` |
| `/aggregated-view?symbol=…&interval=…` | GET | 5m / 15m / 1h aggregations from 1m |
| `/trades` | GET | Trade history |
| `/positions` | GET | Open positions (with `invalidation_levels` on each) |
| `/pnl?period=today\|week\|month` | GET | P&L summary |
| `/decision-log` | GET | LLM decision history |
| `/decision-log/stream` | GET (SSE) | Live decision stream |

Manual scan trigger:
```bash
curl -X POST http://localhost:8001/scan/trigger
```

## Environment variables

### Required

| Variable | Description |
|---|---|
| `FYERS_CLIENT_ID`, `FYERS_SECRET_KEY`, `FYERS_REDIRECT_URI` | Fyers OAuth |
| `LLM_PROVIDER` | `ollama` or `claude` |
| `OLLAMA_MODEL` or `CLAUDE_API_KEY` | Per provider |
| `PROXY_IP`, `PROXY_PORT`, `PROXY_USER`, `PROXY_PASSWORD` | Squid proxy for Fyers REST (WS bypasses) |

### Common tuning

| Variable | Default | Description |
|---|---|---|
| `CANDLE_INTERVAL` | `1m` | `1m` (fetch 1m, aggregate to 5m) or `5m` |
| `MIN_BAR_POSITION` | `2` | Minute into 5m bar before LLM runs |
| `LLM_DECISION_INTERVAL_SECONDS` | `60` | Cadence of the LLM decision cycle (fetch + indicators + LLM call ~29s + persist). <60s queues behind the LLM. Was `SCAN_INTERVAL_SECONDS` pre-WS-migration. |
| `GREEKS_POLL_INTERVAL_SECONDS` | `5` | Cadence of the Greeks (delta/gamma/theta/vega/IV) REST poll for open option positions. Underlying + option *prices* are now WS-fed and don't depend on this. Was `POSITION_WATCHER_INTERVAL_SECONDS` pre-WS-migration. |
| `OLLAMA_TIMEOUT` | `120` | LLM hard timeout (120s for gpt-oss:120b) |
| `INITIAL_BUDGET` | `100000` | Virtual capital (INR, sim mode) |
| `MAX_POSITION_SIZE_PCT` | `10` | Max % of budget per trade |
| `MAX_LOTS` | `5` | Hard cap on lots/trade (DTE-based stricter caps for 0/1/2DTE) |
| `MIN_OPTION_PREMIUM` | `30` | Skip strikes below this premium (₹) |
| `SL_COOLDOWN_MINUTES` | `15` | Block re-entry on underlying after STOP_LOSS / TRAIL_STOP |

## Diagnostics

```bash
docker compose logs -f core-engine          # WS connect/disconnect, scan cycle, LLM calls
docker compose logs -f simulation-engine    # decision consumer, gate blocks, exits
docker exec trading-redis redis-cli         # inspect ltp:* / forming_bar:* directly

# Validate the WS path (run during market hours, inside trading-core)
docker cp tests/fyers_sdk_ws_path_check.py trading-core:/tmp/
docker exec trading-core python /tmp/fyers_sdk_ws_path_check.py

# Compare WS-fed ltp:* vs a parallel REST quote
docker cp tests/ws_rest_shadow_compare.py trading-core:/tmp/
docker exec -d trading-core python /tmp/ws_rest_shadow_compare.py --interval 5 --duration 1800
```

## Deployment

Deploy any service from the currently-checked-out branch:
```bash
./deploy.sh core-engine
./deploy.sh simulation-engine
./deploy.sh api-service
./deploy.sh ui-service
./deploy.sh all
```

`deploy.sh` pushes the current local branch and runs `git checkout -B <branch> origin/<branch>` on the remote — so the remote `git branch` reports the active deployed branch. Rollback is `git checkout master && ./deploy.sh <service>`.
