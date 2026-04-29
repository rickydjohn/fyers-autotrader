# Trading Intelligence System

NSE intraday autotrader using Fyers API, TimescaleDB, and a pluggable LLM backend (Ollama or Claude).

## Architecture

```
┌────────────────────────────────────────────────────────────┐
│                        HOST MACHINE                         │
│  Ollama :11434 (optional)     Browser → localhost:3000      │
└─────────────────────┬──────────────────────────────────────┘
                      │ host.docker.internal
    ┌─────────────────┴──────────────────────────────────┐
    │               trading-net (Docker bridge)           │
    │                                                     │
    │  core-engine:8001 ──XADD──► Redis:6379             │
    │  (Fyers + indicators + LLM)        │                │
    │                           ◄─XREAD─┘                │
    │  simulation-engine:8002                             │
    │  (trade execution, exits, P&L)                      │
    │                                                     │
    │  data-service:8003                                  │
    │  (TimescaleDB ORM, S/R levels, context builder)     │
    │                                                     │
    │  api-service:8000                                   │
    │  (REST + SSE gateway)                               │
    │       ▲                                             │
    │  ui-service:3000                                    │
    │  (React dashboard)                                  │
    │                                                     │
    │  TimescaleDB:5432                                   │
    └─────────────────────────────────────────────────────┘
```

## Services

| Service | Port | Role |
|---|---|---|
| [core-engine](core-engine/README.md) | 8001 | Market data, indicators, LLM decisions, Fyers OAuth |
| [simulation-engine](simulation-engine/README.md) | 8002 | Trade execution (sim or live), exits, P&L, reconciliation |
| [data-service](data-service/README.md) | 8003 | TimescaleDB ORM, historical context, S/R levels, volume profile |
| [api-service](api-service/README.md) | 8000 | REST + SSE gateway |
| [ui-service](ui-service/README.md) | 3000 | React dashboard |

## Setup

### 1. Prerequisites

- Docker + Docker Compose
- Fyers account with API credentials
- Anthropic API key **or** Ollama running locally

### 2. LLM Provider

**Option A — Claude (recommended, no local GPU needed)**

Set in `.env`:
```
LLM_PROVIDER=claude
CLAUDE_API_KEY=sk-ant-...
CLAUDE_MODEL=claude-haiku-4-5-20251001
```

**Option B — Ollama (local, no API cost)**

```bash
ollama pull gemma4:latest
ollama serve   # port 11434
```

Set in `.env`:
```
LLM_PROVIDER=ollama
OLLAMA_MODEL=gemma4:latest
```

### 3. Configure credentials

Edit `.env`:
```
FYERS_CLIENT_ID=xxxxxxxxxxx100
FYERS_SECRET_KEY=xxxxxxxxxx
FYERS_REDIRECT_URI=http://localhost:8001/fyers/callback

INITIAL_BUDGET=100000
SCAN_INTERVAL_SECONDS=60
CANDLE_INTERVAL=1m
MIN_BAR_POSITION=2
```

### 4. Start all services

```bash
docker compose up --build
```

### 5. Authenticate with Fyers (daily)

Open your browser and complete the OAuth flow:
```
http://localhost:8001/fyers/auth
```
The token is saved to a Docker volume and reused across restarts. It expires at 6 AM IST the next day.

### 6. Open the dashboard

```
http://localhost:3000
```

## Trading Logic

Decisions go through three layers before a trade executes.

### Gate 1 — ORB (Opening Range Breakout)
No trades before **09:30 IST**. The first 15 minutes form the opening range; trading is blocked until the range is established.

### Gate 2 — Cross-Symbol Gate (NIFTY → BANK NIFTY)
NIFTY is the lead symbol. Within a 15-minute window:
- Conflicting NIFTY decision **blocks** the dependent symbol's trade.
- Aligned direction **boosts** confidence by +0.08.

### Gate 3 — Entry Proximity Gate
- Block BUY if price is within **0.25%** of day high, prev-day high, or nearest resistance.
- Block SELL if price is within **0.25%** of day low, prev-day low, or nearest support.

### Confidence Floor
A signal must reach **≥ 0.70 confidence** to execute. Anything below is treated as HOLD.

### Three-Layer LLM Decision Framework

```
Layer 1 — Daily Context:
  12-session candle block → macro trend, key daily S/R, CPR position

Layer 2 — Intraday Technicals:
  RSI gates (BUY: 45–70, SELL: 30–55), MACD, VWAP, CPR bands,
  candle patterns (rejection wicks, engulfing, exhaustion),
  volume reversal triggers

Layer 3 — Forming Bar:
  Current incomplete 5m bar read → confidence delta applied (−0.22 to +0.22)

LLM outputs:
  { decision, confidence, reasoning, stop_loss, target, risk_reward,
    candle_summary, session_bias }

Confidence floor applied → trade executes or HOLD
```

## Backtesting

```bash
# LLM decision backtest on historical candles
python core-engine/backtest_candles.py

# Measure cross-symbol gate impact on trade history
python backtest_cross_symbol_gate.py

# Test rule changes against candle data
python backtest_new_rules.py

# Range breakout logic validation
python backtest_range_breakout.py
```

Historical data is fetched via:
```bash
curl -X POST http://localhost:8001/historical/backfill
```

## API Endpoints

Base URL: `http://localhost:8000/api/v1`

| Endpoint | Method | Description |
|---|---|---|
| `/market-data?symbol=NSE:NIFTY50-INDEX` | GET | Live market data + indicators |
| `/trades` | GET | Trade history |
| `/positions` | GET | Open positions |
| `/pnl` | GET | P&L summary |
| `/decision-log` | GET | LLM decision history |
| `/decision-log/stream` | GET (SSE) | Live decision stream |

Manual scan trigger:
```bash
curl -X POST http://localhost:8001/scan/trigger
```

## Environment Variables

### Required

| Variable | Description |
|---|---|
| `FYERS_CLIENT_ID` | Fyers API client ID |
| `FYERS_SECRET_KEY` | Fyers API secret |
| `FYERS_REDIRECT_URI` | OAuth callback URL |
| `LLM_PROVIDER` | `claude` or `ollama` |
| `CLAUDE_API_KEY` | Required when `LLM_PROVIDER=claude` |

### LLM

| Variable | Default | Description |
|---|---|---|
| `CLAUDE_MODEL` | `claude-haiku-4-5-20251001` | Claude model ID |
| `CLAUDE_TIMEOUT` | `30` | Request timeout (s) |
| `OLLAMA_ENDPOINT` | `http://host.docker.internal:11434` | Ollama base URL |
| `OLLAMA_MODEL` | `gemma4:latest` | Ollama model tag |
| `OLLAMA_TIMEOUT` | `45` | Request timeout (s) |

### Trading

| Variable | Default | Description |
|---|---|---|
| `CANDLE_INTERVAL` | `1m` | `1m` (fetch 1m, aggregate to 5m) or `5m` (fetch 5m directly) |
| `MIN_BAR_POSITION` | `2` | Minutes into current 5m bar before LLM runs (0–4). Combine with `CANDLE_INTERVAL=5m` and set to `4` to replicate pre-1m behaviour. |
| `SCAN_INTERVAL_SECONDS` | `60` | How often to scan |
| `INITIAL_BUDGET` | `100000` | Virtual capital (INR) |
| `MAX_POSITION_SIZE_PCT` | `10` | Max % of budget per trade |
| `SLIPPAGE_PCT` | `0.05` | Entry/exit slippage |
| `COMMISSION_FLAT` | `20` | Min commission per trade (INR) |
| `MIN_OPTION_PREMIUM` | `30` | Skip option strikes below this premium (₹) |
| `SL_COOLDOWN_MINUTES` | `15` | Block re-entry on underlying after stop-loss |

### Proxy (optional)

Set all four to route Fyers traffic through a proxy (IPv4-forced):
```
PROXY_IP, PROXY_PORT, PROXY_USER, PROXY_PASSWORD
```

## Logs

```bash
docker compose logs -f core-engine
docker compose logs -f simulation-engine
docker compose logs -f data-service
```
