# data-service

The persistence layer. Owns all TimescaleDB reads and writes, builds historical context snapshots for the LLM, serves aggregated candle views, and generates monthly reports.

**Port:** `8003`

## Responsibilities

- Candle ingestion (single + batch upsert into `market_candles` TimescaleDB hypertable)
- LLM decision persistence and retrieval
- Historical context snapshots for Layer 1 LLM context (Redis-cached, DB fallback)
- Support/resistance level storage and bulk upsert
- Daily indicator storage (CPR, pivot levels, prev-day OHLC)
- Monthly P&L report generation

## Key environment variables

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://trading:trading@timescaledb:5432/trading` | TimescaleDB connection string |
| `REDIS_URL` | `redis://redis:6379` | Redis connection for context snapshot cache |
| `CONTEXT_LOOKBACK_DAYS` | `5` | Days of candle history included in each LLM context snapshot |

See [`EXAMPLE.env`](../EXAMPLE.env) for the full variable list.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for component map, database schema, and repository layer design.
