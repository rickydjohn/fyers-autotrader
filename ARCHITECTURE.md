# System Architecture

## Service Interaction Overview

```mermaid
graph TD
    FYERS_REST["Fyers REST\n(quotes, history, orders)"]
    FYERS_WS["Fyers WebSocket\n(socket.fyers.in)"]
    OLLAMA["Ollama gpt-oss:120b-cloud\nor Claude Haiku 4.5"]
    REDIS["Redis :6379\n(Cache + Streams)"]
    TSDB["TimescaleDB :5432\n(Persistent Storage)"]
    SQUID["Squid proxy\n(Fyers REST egress only)"]

    CORE["Core Engine :8001\nFyersTickFeed · Scheduler · LLM"]
    SIM["Simulation Engine :8002\nGates · Exits · Invalidation Watcher"]
    DATA["Data Service :8003\nORM · Context · History"]
    API["API Service :8000\nREST + SSE gateway"]
    UI["UI Service :3000\nReact dashboard"]

    FYERS_REST -->|"quotes, 1m candles, Greeks"| SQUID
    SQUID --> CORE
    FYERS_WS -->|"single WS, N symbols multiplexed"| CORE

    CORE -->|"market: snapshot\nltp:* (WS-fed, 200ms)\nforming_bar:* / last_bar:*\ndecisions stream"| REDIS
    CORE -->|"POST /ingest/candle\nPOST /ingest/decision"| DATA
    CORE <-->|"GET /context-snapshot"| DATA
    OLLAMA <-->|"prompt / decision"| CORE

    REDIS -->|"decisions stream (xread)\nltp:* + market: for fresh-price gates"| SIM
    SIM -->|"positions:open\ntrades:all\nbudget:state"| REDIS
    SIM -->|"POST /ingest/trade"| DATA
    FYERS_REST <-->|"place/check orders (live mode)"| SIM

    DATA <-->|"SQLAlchemy async ORM"| TSDB

    REDIS -->|"market:*  ltp:*\nforming_bar:* / last_bar:*\ntrades / decisions / pnl"| API
    API <-->|"HTTP proxy"| SIM
    API <-->|"HTTP proxy"| CORE
    API <-->|"HTTP proxy"| DATA

    API -->|"REST JSON\n+ /forming-bar (1Hz)"| UI
    API -->|"SSE decision stream"| UI
```

**Network topology note.** REST traffic to Fyers goes through Squid (the proxy IP is on Fyers' whitelist). The WebSocket goes **direct to Cloudflare** — Fyers' WS endpoint isn't IP-whitelisted, and the SDK's `WebSocketApp.run_forever` path doesn't pass proxy params anyway. Verified end-to-end 2026-05-14 via `tests/fyers_sdk_ws_path_check.py` and Squid access logs.

## Data Flow: Decision → Trade

```mermaid
sequenceDiagram
    participant F as Fyers (REST + WS)
    participant C as Core Engine
    participant O as LLM
    participant R as Redis
    participant S as Sim Engine

    Note over F,C: WS background — continuous
    F-->>C: tick (symbol, ltp, exch_feed_time)
    C->>R: SETEX ltp:{symbol} (200ms throttle)
    C->>R: SETEX forming_bar:{symbol} (200ms)
    Note over C: on minute rollover
    C->>R: SETEX last_bar:{symbol} (120s TTL)

    Note over F,S: LLM decision cycle — every LLM_DECISION_INTERVAL_SECONDS
    C->>F: GET /quotes + /history (via Squid)
    F-->>C: snapshot + 1m candles
    C->>C: indicators (CPR, RSI, EMA, VWAP, MACD)
    C->>O: prompt (snapshot + 12-day context + sector breadth)
    O-->>C: { decision, confidence, reasoning, sl, target }
    C->>R: XADD decisions stream
    C->>R: SETEX market:{symbol}  (60s scan snapshot)

    R-->>S: XREAD decisions
    S->>R: GET ltp:{symbol}  (fresh, may have ws_ltp)
    S->>S: drift veto (snapshot vs live)
    S->>S: ORB / CPR / consolidation / proximity gates (live price)
    S->>S: invalidation_levels captured from indicators_snapshot
    S->>R: HSET positions:open (Position + invalidation_levels)

    Note over S: every consumer tick
    S->>R: GET ltp:{symbol} per open position
    S->>S: check_invalidation_exit (vwap / ema21 / cpr cross)
    S->>S: check_exit (premium SL, trail, session close)
    S->>F: place exit order (live) / record (sim)
```

## Three-Layer Entry Pipeline (simulation-engine consumer)

```
LLM decision
   │
   ├─ Confidence floor (≥ 0.70)
   ├─ Late-session cutoff — block new entries within 15 min of session_close
   ├─ Fresh-price refresh — fetch live ltp and use it for the downstream gates
   │                       (gates evaluate against the actual market, not the
   │                        scan-time snapshot which may be tens of seconds stale)
   ├─ ORB gate    — price must clear 09:15–09:30 range by ORB_BUFFER
   │                (gate is disabled for the rest of the session once price
   │                 has crossed either threshold today — backtest shows
   │                 ~75% of break-days have material follow-through)
   ├─ CPR gate    — block when price is inside the [BC × 0.998, TC × 1.002]
   │                no-trade bracket; outside, direction-agnostic (CPR is a
   │                level, not a breakout barrier)
   ├─ Consolidation gate — block when inside a tight consolidation AND
   │                       (no breakout OR breakout direction conflicts with signal)
   ├─ Entry proximity   — block if next level is within PA_PROXIMITY
   ├─ Pre-entry exit sim — run check_exit on a hypothetical position with
   │                       a 0.5% favorable tick; if any exit/trail would
   │                       fire on tick 1, refuse the entry (catches
   │                       entry-vs-exit gate definition inconsistencies)
   └─ open_position  (captures invalidation_levels from snapshot)
```

## Three-Layer Exit Pipeline (per consumer tick)

```
For each open position:
   │
   ├─ check_invalidation_exit  (cheap; runs first)
   │     SELL → exit if price > vwap / ema_21 / cpr_tc
   │     BUY  → exit if price < vwap / ema_21 / cpr_bc
   │
   └─ check_exit  (the heavier rule chain)
         ├─ session close (15:20 hard exit)
         ├─ premium stop loss (−10% on option)
         ├─ milestone trail (locks profit after +20% / +10% on day_type)
         └─ option-LTP unavailable → skip cycle (don't fall back to underlying)
```

## Redis Key Space

```mermaid
graph LR
    subgraph "Core Engine writes"
        MKT["market:{symbol}\nJSON · 600s — scan snapshot"]
        CTX["context:{symbol}\nJSON · 300s"]
        NS["news:sentiment\nJSON · 3600s"]
        DST["decisions\nXSTREAM · maxlen 1000"]
        DL["decision:log\nZSET"]
    end

    subgraph "FyersTickFeed writes (WS, sub-second)"
        LTP["ltp:{symbol}\n{ltp, ts} · 30s · 200ms throttle"]
        FB["forming_bar:{symbol}\n{O,H,L,C,n_ticks} · 90s"]
        LB["last_bar:{symbol}\n{O,H,L,C} · 120s (on minute roll)"]
    end

    subgraph "Fast Watcher writes"
        OLTP["ltp:{option_symbol}\n{ltp, ts} · 30s — every 5s when positions open"]
        GRK["greeks:{option_symbol}\nJSON · 30s"]
    end

    subgraph "Simulation Engine writes"
        POS["positions:open\nHash · Position incl. invalidation_levels"]
        TRD["trades:all\nHash"]
        OID["trades:open_id\nHash (symbol→trade_id)"]
        BST["budget:state\nJSON"]
        SLC["sl:cooldown:{symbol}\n900s — STOP_LOSS only"]
    end

    subgraph "API Service reads"
        MKT --> API_R["API :8000"]
        LTP --> API_R
        FB --> API_R
        LB --> API_R
        TRD --> API_R
        POS --> API_R
        BST --> API_R
    end
```

**Overlay rule on `/market-data`**: the endpoint reads `market:{symbol}` for the full snapshot, then if `ltp:{symbol}` has `ts` ≤ 30s old, replaces `ltp` with the WS value and adds `ltp_source: "ws"`. Fail-open: any error returns the unaltered snapshot.

## Forming-Bar Pipeline (chart consolidation at 1 Hz)

```
WS tick (exch_feed_time)             SDK thread
   │  bar_min = (exch_feed_time // 60) * 60
   ▼
self._forming_bars[symbol]  =  {bar_min, O, H, L, C, n}
   │  (on minute rollover: previous → self._finalized_bars[symbol])
   ▼
async consumer (200ms throttle per symbol)         event loop
   │  SETEX forming_bar:{symbol}  (90s TTL)
   │  SETEX last_bar:{symbol}     (120s TTL on rollover)
   ▼
GET /api/v1/market-data/forming-bar
   │  returns { forming_bar, last_bar }
   ▼
ui-service useEffect (1 Hz, every timeframe)
   │  1m  : splice forming bar as its own candle
   │  ≥5m : update LAST aggregated candle's close, extend H/L
   ▼
React state setter returns the original array when nothing changed → no re-render
```

The 200ms backend throttle and 1s UI poll are deliberately decoupled. Backend stays fast so the **drift veto** and **invalidation exits** see fresh prices; UI is consolidated to 1Hz so the chart never flickers tick-by-tick.

## Database Schema (TimescaleDB)

```mermaid
erDiagram
    market_candles {
        timestamptz time PK
        text symbol PK
        float open
        float high
        float low
        float close
        bigint volume
        float vwap
        float rsi
        float ema_9
        float ema_21
    }
    daily_indicators {
        date date PK
        text symbol PK
        float prev_high
        float prev_low
        float prev_close
        float pivot
        float bc
        float tc
        float r1
        float r2
        float r3
        float s1
        float s2
        float s3
        float cpr_width_pct
    }
    ai_decisions {
        uuid decision_id PK
        timestamptz time
        text symbol
        text decision
        float confidence
        text reasoning
        jsonb indicators_snapshot
        float stop_loss
        float target
        boolean acted_upon
        text option_symbol
        float option_price
    }
    trades {
        uuid trade_id PK
        text symbol
        text side
        int quantity
        float entry_price
        timestamptz entry_time
        float exit_price
        timestamptz exit_time
        float pnl
        float pnl_pct
        text status
        text exit_reason
        text trading_mode
        text option_symbol
        uuid decision_id FK
    }
    daily_ohlcv {
        date date PK
        text symbol PK
    }
    historical_sr_levels {
        text symbol
        float level
        float strength
    }

    ai_decisions ||--o{ trades : "decision_id"
```

**Bar-persistence note.** The scheduler's filter for which 1m bars to upsert into `market_candles` uses `c.timestamp >= last_ts` (not strict `>`) so a bar that was persisted while partial gets one more upsert when Fyers finalises it. Without this, the DB held point-snapshots with 10-20pt phantom gaps between consecutive bars. Helper lives in `core-engine/scheduler/candle_filter.py` with unit tests in `tests/core/test_candle_persistence_filter.py`. The WS tick feed never writes to TimescaleDB — only Redis.

## Diagnostic / fixture assets

- `tests/fyers_sdk_ws_path_check.py` — proves which network path the SDK uses (REST → Squid, WS → direct).
- `tests/ws_proxy_smoketest.py` — validates Squid CONNECT to wss://socket.fyers.in works.
- `tests/ws_rest_shadow_compare.py` — runs inside trading-core during market hours, compares WS-fed `ltp:*` against a parallel REST `/quotes` for parity + latency.
- `tests/fixtures/ws_capture_2026-05-15/` — 5 min of live WS frames, reference API responses, and an offline analyser. Used to validate the forming-bar / tick-driven-exit design without needing market hours.
