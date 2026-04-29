# ui-service

React + TypeScript SPA. Polls REST endpoints for positions, trades, and P&L, connects to the SSE stream for live LLM decisions, and presents everything in a single-page dashboard with an optional reports view.

**Port:** `3000`

## Responsibilities

- Live price header (LTP, change %, RSI, CPR position, VWAP)
- Candlestick chart with timeframe selector (1m, 5m, 15m, 1h, 1d)
- Open positions panel and trade history table
- P&L summary card
- LLM decision log with live SSE updates
- Trading mode toggle (simulation / live)
- Monthly reports view

## Development

```bash
cd ui-service
npm install
npm run dev   # http://localhost:3000
```

The UI expects the api-service at `http://localhost:8000`. When running via Docker Compose this is handled automatically.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for component tree, data flow, and API integration points.
