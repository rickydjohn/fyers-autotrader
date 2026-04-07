# Data Service — Architecture

The data service is the persistence layer. It owns all TimescaleDB reads and writes, builds historical context snapshots for the LLM, serves aggregated candle views, and generates monthly reports.

## Component Map

```mermaid
graph TD
    subgraph "FastAPI Routers (routers/)"
        INGEST["ingest.py\nPOST candle · decision\ntrade · news · daily-indicator\nbatch candles"]
        HIST["historical.py\nGET historical-data\nGET aggregated-view\nGET daily-ohlcv"]
        CTX_R["context.py\nGET context-snapshot\n(Redis cache → DB fallback)"]
        DH["decision_history.py\nGET decisions\npagination + filters"]
        RPT["report.py\nGET report/trades\nmonthly P&L summary"]
        SR_R["sr_levels.py\nGET sr-levels\nPOST sr-levels (bulk upsert)"]
    end

    subgraph "Repositories (repositories/)"
        MR["market_data.py\ninsert_candle()\nget_candles(symbol, interval, from, to)"]
        DR["decisions.py\ninsert_decision()\nget_decisions(symbol, limit)\nmark_acted_upon()"]
        TR["trades.py\nupsert_trade()\nget_trades(symbol, mode, limit)\nget_monthly_trades()"]
        NR["news.py\ninsert_news_batch()\nget_recent_news()"]
        SRR["sr_levels.py\nbulk_upsert_levels()\nget_levels(symbol, near_price)"]
    end

    subgraph "Context Builder (context/)"
        CB["builder.py\nbuild_context_snapshot()\nformat_context_for_prompt()"]
    end

    subgraph "DB Layer (db/)"
        CONN["connection.py\nAsyncEngine\nAsyncSessionLocal"]
        MODELS["models.py\nSQLAlchemy ORM models"]
    end

    subgraph "External"
        TSDB["TimescaleDB :5432"]
        REDIS_D["Redis :6379\n(context cache)"]
        CORE["Core Engine :8001\n(consumer)"]
        SIM["Simulation Engine :8002\n(consumer)"]
        API["API Service :8000\n(consumer)"]
    end

    CORE -->|"POST /ingest/candle\nPOST /ingest/decision\nPOST /ingest/news"| INGEST
    SIM -->|"POST /ingest/trade"| INGEST
    CORE -->|"GET /context-snapshot\nGET /sr-levels"| CTX_R
    API -->|"GET /historical-data\nGET /aggregated-view\nGET /decision-history\nGET /report/trades"| HIST
    API --> DH
    API --> RPT

    INGEST --> MR
    INGEST --> DR
    INGEST --> TR
    INGEST --> NR

    CTX_R --> CB
    CB --> MR
    CTX_R -->|"cache read/write"| REDIS_D

    HIST --> MR
    DH --> DR
    RPT --> TR
    SR_R --> SRR

    MR --> CONN
    DR --> CONN
    TR --> CONN
    NR --> CONN
    SRR --> CONN

    CONN --> MODELS
    CONN --> TSDB
```

## Context Snapshot Build Flow

```mermaid
flowchart TD
    REQ(["GET /context-snapshot?symbol=X"]) --> CACHE{Redis cache hit?\ncontext:{symbol} · 5min TTL}
    CACHE -->|hit| RETURN_CACHE([Return cached JSON])
    CACHE -->|miss| QUERY[Query market_candles\nlast 5 trading days]

    QUERY --> AGG_5M["Aggregate 1m → 5m candles\n(OHLCV grouping)"]
    AGG_5M --> AGG_15M["Aggregate → 15m candles"]
    AGG_15M --> AGG_1H["Aggregate → 1h candles"]
    AGG_1H --> AGG_D["Aggregate → daily candles"]

    AGG_D --> INDICATORS["Compute per-timeframe\nRSI · MACD · EMA · trend direction"]
    INDICATORS --> FORMAT["Format structured context\nfor LLM prompt injection"]
    FORMAT -->|"SET context:{symbol}\n300s TTL"| REDIS_D["Redis"]
    FORMAT --> RETURN([Return context JSON])
```

## Ingest Pipeline

```mermaid
flowchart LR
    subgraph "Candle Ingest"
        C1["POST /ingest/candle"] --> C2["Upsert market_candles\n(ON CONFLICT time, symbol DO UPDATE)"]
        C3["POST /ingest/candles (batch)"] --> C2
    end

    subgraph "Decision Ingest"
        D1["POST /ingest/decision"] --> D2["INSERT ai_decisions\n(ON CONFLICT decision_id DO NOTHING)"]
        D3["POST /ingest/decision/{id}/acted"] --> D4["UPDATE ai_decisions\nSET acted_upon=true"]
    end

    subgraph "Trade Ingest"
        T1["POST /ingest/trade"] --> T2["UPSERT trades\n(ON CONFLICT trade_id DO UPDATE\nexit_price, pnl, pnl_pct, status,\nexit_time, exit_reason, commission)"]
    end

    subgraph "News Ingest"
        N1["POST /ingest/news (batch)"] --> N2["Dedup within batch\n(lowercase + trim)"]
        N2 --> N3["Query DB last 48h titles\n(application-level dedup)"]
        N3 --> N4["INSERT new items only\ninto news_items hypertable"]
    end
```

## Monthly Report Generation

```mermaid
flowchart TD
    REQ(["GET /report/trades?month=YYYY-MM"]) --> QUERY["Query trades table\nWHERE entry_time BETWEEN\nmonth_start AND month_end"]

    QUERY --> CLOSED["Filter: status != OPEN"]

    CLOSED --> WINNERS["winners = pnl > 0"]
    CLOSED --> LOSERS["losers = pnl ≤ 0"]

    WINNERS --> GROSS_P["gross_profit = sum(winners.pnl)"]
    LOSERS --> GROSS_L["gross_loss = sum(losers.pnl)"]
    CLOSED --> NET["net_pnl = sum(all closed.pnl)\n(includes both entry + exit commissions)"]

    CLOSED --> BY_REASON["Group by exit_reason\n{ pnl, count, wins, losses }"]

    GROSS_P & GROSS_L & NET & BY_REASON --> RESPONSE["Return:\nsummary + by_exit_reason + trades[]"]
```

## TimescaleDB Continuous Aggregates

```mermaid
graph TD
    RAW["market_candles\n(1m raw ticks)"] -->|"auto-aggregate"| CA5["candles_5m\nmaterialized view"]
    RAW -->|"auto-aggregate"| CA15["candles_15m\nmaterialized view"]
    RAW -->|"auto-aggregate"| CA1H["candles_1h\nmaterialized view"]
    RAW -->|"auto-aggregate"| CAD["candles_daily\nmaterialized view"]

    CA5 -->|"GET /aggregated-view?interval=5m"| API_H["API /historical-data"]
    CA15 -->|"GET /aggregated-view?interval=15m"| API_H
    CA1H -->|"GET /aggregated-view?interval=1h"| API_H
    CAD -->|"GET /aggregated-view?interval=1d"| API_H
    RAW -->|"GET /historical-data?interval=1m"| API_H
```
