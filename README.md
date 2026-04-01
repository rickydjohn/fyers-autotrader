# Trading Intelligence System

Modular trading system using Fyers API + Ollama LLM for NIFTY/BANK NIFTY analysis.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   HOST MACHINE                       │
│  Ollama :11434        Browser → localhost:3000       │
└──────────────────┬──────────────────────────────────┘
                   │ host.docker.internal
    ┌──────────────┴─────────────────────────────┐
    │              trading-net (Docker bridge)    │
    │                                             │
    │  core-engine:8001  ──XADD──►  Redis:6379   │
    │  (Fyers + indicators + Ollama)   │          │
    │                                  ◄─XREAD─  │
    │  simulation-engine:8002           │          │
    │  (mock trades, P&L)              │          │
    │                        ◄─────────┘          │
    │  api-service:8000                           │
    │  (REST + SSE)                               │
    │       ▲                                     │
    │  ui-service:3000                            │
    └─────────────────────────────────────────────┘
```

## Services

| Service | Port | Role |
|---|---|---|
| core-engine | 8001 | Market data, indicators, LLM decisions |
| simulation-engine | 8002 | Virtual trading, P&L |
| api-service | 8000 | REST + SSE gateway |
| ui-service | 3000 | React dashboard |

## Setup

### 1. Prerequisites

- Docker + Docker Compose
- [Ollama](https://ollama.ai) running locally
- Fyers account with API credentials

### 2. Install Ollama model

```bash
ollama pull llama3.2:3b
ollama serve   # ensure it's running on port 11434
```

### 3. Configure credentials

Edit `.env` (already pre-filled with your Fyers credentials):
```
FYERS_CLIENT_ID=xxxxxxxxxxx100
FYERS_SECRET_KEY=xxxxxxxxxx
INITIAL_BUDGET=100000
```

### 4. Start all services

```bash
docker compose up --build
```

### 5. Authenticate with Fyers

Open your browser:
```
http://localhost:8001/fyers/auth
```
Complete the OAuth login. Token is saved to a Docker volume and reused.

### 6. Open the dashboard

```
http://localhost:3000
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

## Trading Logic

```
IF price > CPR_TC AND RSI in 45-65 AND sentiment BULLISH → BUY
IF price < CPR_BC AND RSI in 35-55 AND sentiment BEARISH → SELL
IF price INSIDE_CPR OR RSI extreme OR conflicting signals → HOLD

LLM validates the rule-based signal with news context and outputs:
  { decision, confidence, reasoning, stop_loss, target, risk_reward }

Trades only execute if confidence >= 0.5
```

## Simulation Config

| Variable | Default | Description |
|---|---|---|
| `INITIAL_BUDGET` | 100000 | Virtual capital (INR) |
| `MAX_POSITION_SIZE_PCT` | 10 | Max % of budget per trade |
| `SLIPPAGE_PCT` | 0.05 | Entry/exit slippage |
| `COMMISSION_FLAT` | 20 | Min commission per trade (INR) |
| `SCAN_INTERVAL_SECONDS` | 300 | How often to scan (seconds) |

## Manual scan trigger

```bash
curl -X POST http://localhost:8001/scan/trigger
```

## Logs

```bash
docker compose logs -f core-engine
docker compose logs -f simulation-engine
```
