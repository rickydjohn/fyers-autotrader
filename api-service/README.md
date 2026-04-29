# api-service

Single entry point for the UI. Acts as a gateway — reading from Redis for real-time state, proxying historical and report queries to the data-service, and streaming live decisions to the UI via SSE.

**Port:** `8000`

## Responsibilities

- Live market data and indicator snapshot (sourced from Redis)
- Trade history and open position queries (proxied to data-service)
- P&L summary endpoint
- LLM decision log — paginated history and live SSE stream
- Trading mode toggle (simulation ↔ live)
- Historical candle and context-snapshot proxy

## Key environment variables

| Variable | Default | Description |
|---|---|---|
| `DATA_SERVICE_URL` | `http://data-service:8003` | Internal URL for data-service |
| `CORE_ENGINE_URL` | `http://core-engine:8001` | Internal URL for core-engine |
| `SIM_ENGINE_URL` | `http://simulation-engine:8002` | Internal URL for simulation-engine |
| `REDIS_URL` | `redis://redis:6379` | Redis connection for live state reads |

See [`EXAMPLE.env`](../EXAMPLE.env) for the full variable list.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for component map, router breakdown, and Redis/data-service interaction patterns.
