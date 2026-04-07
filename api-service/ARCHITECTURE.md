# API Service — Architecture

The API service is the single entry point for the UI. It acts as a gateway — reading from Redis for real-time state, proxying historical and report queries to the data service, and streaming live decisions to the UI via SSE.

## Component Map

```mermaid
graph TD
    subgraph "FastAPI Routers (routers/)"
        MKT["market_data.py\nGET /market-data\nGET /market-data/symbols"]
        TRD["trades.py\nGET /trades"]
        POS["positions.py\nGET /positions"]
        PNL["pnl.py\nGET /pnl"]
        DL["decision_log.py\nGET /decision-log\nGET /decision-log/stream (SSE)"]
        MODE["trading_mode.py\nGET /trading-mode\nPOST /trading-mode"]
        HIST_P["historical.py\nGET /historical-data\nGET /aggregated-view\nGET /context-snapshot\nGET /decision-history"]
        RPT_P["report.py\nGET /report/trades"]
    end

    subgraph "Dependencies (dependencies.py)"
        DEP_R["get_redis()\nshared AsyncRedis client"]
        DEP_H["get_http_client()\nhttpx.AsyncClient\n→ core-engine\n→ sim-engine\n→ data-service"]
    end

    subgraph "External Services"
        REDIS["Redis :6379"]
        CORE["Core Engine :8001"]
        SIM["Simulation Engine :8002"]
        DATA["Data Service :8003"]
    end

    UI["UI Service :3000"] -->|"REST JSON"| MKT
    UI -->|"REST JSON"| TRD
    UI -->|"REST JSON"| POS
    UI -->|"REST JSON"| PNL
    UI -->|"REST JSON"| DL
    UI -->|"SSE stream"| DL
    UI -->|"REST JSON"| MODE
    UI -->|"REST JSON"| HIST_P
    UI -->|"REST JSON"| RPT_P

    MKT --> DEP_R
    TRD --> DEP_R
    POS --> DEP_R
    PNL --> DEP_R
    DL --> DEP_R
    MODE --> DEP_R

    HIST_P --> DEP_H
    RPT_P --> DEP_H
    POS --> DEP_H

    DEP_R --> REDIS
    DEP_H -->|"GET /context-snapshot\nGET /historical-data\nGET /decision-history\nGET /report/trades"| DATA
    DEP_H -->|"GET /positions /pnl /budget"| SIM
    DEP_H -->|"GET /fyers/funds\nGET /fyers/orders/{id}/status"| CORE
```

## Request Flow per Endpoint

```mermaid
flowchart TD
    subgraph "GET /market-data"
        MD1["HGETALL market:*\nfrom Redis"] --> MD2["Return all symbol snapshots\n(ltp, change, indicators, candles)"]
    end

    subgraph "GET /positions"
        P1["HGETALL positions:open\nfrom Redis"] --> P2["Enrich each position\nwith current LTP"]
        P2 --> P3["Return positions[]\nwith unrealized P&L"]
    end

    subgraph "GET /trades"
        T1["ZREVRANGE trades:history\nfrom Redis (sorted set)"] --> T2["Filter by symbol / limit"]
        T2 --> T3["Return trades[]"]
    end

    subgraph "GET /pnl"
        PL1["GET pnl:realized:total from Redis"] --> PL2["GET budget:state from Redis"]
        PL2 --> PL3["Compute win_rate, avg_win, avg_loss\nfrom trades:history"]
        PL3 --> PL4["Build P&L timeline\nfrom pnl:daily:{date}"]
        PL4 --> PL5["Return PnLResponse"]
    end

    subgraph "GET /decision-log"
        DL1["ZREVRANGEBYSCORE decision:log\nfrom Redis"] --> DL2["Decode JSON\nApply symbol / decision filters"]
        DL2 --> DL3["Return decisions[] + total"]
    end

    subgraph "SSE /decision-log/stream"
        SSE1["Open SSE connection"] --> SSE2["Poll decision:log\nevery 2s (ZRANGEBYSCORE\nby last_id)"]
        SSE2 -->|"new entry"| SSE3["Emit 'decision' event"]
        SSE2 -->|"15s idle"| SSE4["Emit 'heartbeat' event"]
        SSE3 & SSE4 --> SSE2
    end

    subgraph "GET /historical-data or /aggregated-view"
        H1["HTTP proxy → Data Service\n/historical-data or /aggregated-view"] --> H2["Return candles[]"]
    end

    subgraph "GET /report/trades"
        R1["HTTP proxy → Data Service\n/report/trades?month=YYYY-MM"] --> R2["Return summary + breakdown + trades[]"]
    end
```

## SSE Decision Stream Detail

```mermaid
sequenceDiagram
    participant UI as UI Service
    participant API as API Service
    participant R as Redis

    UI->>API: GET /api/v1/decision-log/stream
    API-->>UI: HTTP 200 Content-Type: text/event-stream

    loop every 2 seconds
        API->>R: ZRANGEBYSCORE decision:log (last_id, +inf)
        R-->>API: new decisions (if any)
        alt new decisions found
            API-->>UI: event: decision\ndata: {...}
        else 15s idle
            API-->>UI: event: heartbeat\ndata: {"ts": "..."}
        end
    end

    UI->>API: connection closed (navigate away / error)
    API->>API: cleanup generator
```

## Trading Mode Toggle

```mermaid
flowchart LR
    UI_TOGGLE["UI TradingModeToggle\nbutton"] -->|"POST /trading-mode {mode: live}"| MODE_R["trading_mode.py"]
    MODE_R -->|"SET trading:mode live"| REDIS["Redis"]
    REDIS -->|"GET trading:mode"| SIM["Simulation Engine\nreads on each decision"]
    SIM -->|"simulation"| MOCK["mock_broker.open_position()"]
    SIM -->|"live"| LIVE["live_broker.open_position()\n→ Fyers place_order()"]
```
