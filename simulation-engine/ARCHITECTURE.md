# Simulation Engine — Architecture

The simulation engine consumes decisions from the Redis stream, manages virtual (or live) trade execution, enforces exit rules on every position watcher cycle, and tracks P&L via the budget system.

## Component Map

```mermaid
graph TD
    subgraph "Entry Points"
        LIFE["main.py\nlifespan hook"]
        CONSUMER["consumer loop\n(async task)"]
    end

    subgraph "Execution (execution/)"
        MOCK["mock_broker.py\nopen_position()\nclose_position()"]
        LIVE["live_broker.py\nopen_position()\nclose_position()\n_await_fill()"]
        EXIT["exit_rules.py\ncheck_exit()\n_indicators_confirm()"]
    end

    subgraph "Portfolio (portfolio/)"
        BUDGET["budget.py\nallocate()\nrelease()\nget_state()"]
    end

    subgraph "Models (models/)"
        SCHEMAS["schemas.py\nPosition\nTrade\nBudgetState\nPnLSnapshot"]
    end

    subgraph "Analytics"
        PNL_ANA["analytics/pnl.py\naggregate_pnl()\ntrade_stats()"]
    end

    subgraph "External I/O"
        REDIS["Redis"]
        FYERS_API["Fyers API\n(live mode only)"]
        DATASVC["Data Service :8003"]
        CORE_API["Core Engine :8001\n/fyers/orders/{id}/status"]
    end

    LIFE --> CONSUMER
    CONSUMER -->|"XREAD decisions\nblock=5s"| REDIS
    REDIS --> CONSUMER

    CONSUMER -->|"BUY/SELL decision"| BROKER{trading_mode?}
    BROKER -->|simulation| MOCK
    BROKER -->|live| LIVE

    MOCK --> BUDGET
    LIVE --> BUDGET
    LIVE --> CORE_API
    LIVE --> FYERS_API

    MOCK -->|"HSET positions:open\nHSET trades:all\nHSET trades:open_id"| REDIS
    MOCK -->|"POST /ingest/trade"| DATASVC

    CONSUMER -->|"every cycle\nfor each open position"| EXIT
    EXIT -->|"GET ltp:{symbol}\nGET greeks:{option}"| REDIS
    EXIT -->|"close signal"| MOCK
    EXIT -->|"close signal"| LIVE

    BUDGET -->|"HSET budget:state"| REDIS
    MOCK -->|"SET pnl:realized:total"| REDIS
```

## Trade Lifecycle

```mermaid
flowchart TD
    DECISION(["Decision arrives\n(BUY/SELL from stream)"]) --> G1{Session close check\n≥ 15:15 IST?}
    G1 -->|yes| SKIP1([Skip — session ending])
    G1 -->|no| G2{Min option premium\n≥ ₹30?}
    G2 -->|no| SKIP2([Skip — premium too low])
    G2 -->|yes| G3{SL cooldown active\nfor symbol?}
    G3 -->|yes| SKIP3([Skip — cooling down\nafter stop loss])
    G3 -->|no| G4{Open position already\nexists for symbol?}
    G4 -->|yes| SKIP4([Skip — no double position])
    G4 -->|no| OPEN[open_position()]

    OPEN --> SLIP[Apply slippage\n±0.05% of price]
    SLIP --> COMM[Calculate commission\nmax₹20 flat · 0.03% of value]
    COMM --> ALLOC{Budget allocation\nsufficient?}
    ALLOC -->|no| SKIP5([Skip — insufficient budget])
    ALLOC -->|yes| PERSIST[Persist Position + Trade\nto Redis + TimescaleDB]
    PERSIST --> ACTIVE(["Position OPEN\nmonitored every 10s"])

    ACTIVE --> EXIT_CHECK[check_exit() called]

    EXIT_CHECK --> SC{Rule 1\nSession close\n≥ 15:00 IST?}
    SC -->|yes| CLOSE_SC([Close: SESSION_CLOSE])

    SC -->|no| SL{Rule 2\nOption LTP ≤\nentry × 0.90?}
    SL -->|yes| CLOSE_SL([Close: STOP_LOSS\n+ set 5min cooldown])

    SL -->|no| DELTA{Rule 3\n|delta| < 0.20?}
    DELTA -->|yes| CLOSE_DE([Close: DELTA_ERODED])

    DELTA -->|no| IV{Rule 4\nIV < entry_IV × 0.80?}
    IV -->|yes| CLOSE_IV([Close: IV_CRUSH])

    IV -->|no| TRAIL{Rule 5\nLTP ≤ peak × 0.95\nAND milestone ≥ 1?}
    TRAIL -->|yes| CLOSE_TS([Close: TRAIL_STOP])

    TRAIL -->|no| MILE{Rule 6\nLTP ≥ entry × milestone_pct?}
    MILE -->|no| ACTIVE
    MILE -->|yes| CONFIRM{2-of-3 indicators confirm?\nRSI · VWAP · MACD}
    CONFIRM -->|yes| ADV[Advance milestone\nUpdate peak price]
    ADV --> ACTIVE
    CONFIRM -->|no| CLOSE_MILE([Close: milestone\ngains locked])
```

## Exit Rule Priority

```mermaid
graph TD
    E1["① SESSION_CLOSE\n≥ 15:00 IST\n(hard, no override)"]
    E2["② STOP_LOSS\nOption LTP ≤ entry × 0.90\n(hard premium floor)"]
    E3["③ DELTA_ERODED\n|delta| < 0.20\n(option far OTM)"]
    E4["④ IV_CRUSH\nIV < entry_IV × 0.80\n(vega decay)"]
    E5["⑤ TRAIL_STOP\nLTP ≤ peak × 0.95\n(after milestone 1)"]
    E6["⑥ MILESTONE_CLOSE\nprofit confirmed but\nindicators don't continue"]
    E7["⑦ TARGET / SL\nfor underlying trades"]

    E1 --> E2 --> E3 --> E4 --> E5 --> E6 --> E7
```

## Budget Flow

```mermaid
flowchart LR
    INIT["initial_budget\n₹100,000"] --> CASH["cash\n(available)"]
    CASH -->|"allocate(invest_amount, fee)"| INV["invested\n(locked in position)"]
    CASH -->|"entry commission\n(exits cash, not invested)"| FEE_OUT["commission cost"]
    INV -->|"release(invested, budget_pnl)\n= exit price × qty − exit_commission"| CASH
    INV -->|"exit commission\ndeducted from budget_pnl"| FEE_OUT

    note["trade.pnl stored in DB\n= gross_pnl − entry_comm − exit_comm\n(true net for reporting)\n\nbudget_pnl passed to release()\n= gross_pnl − exit_comm only\n(entry commission already gone)"]
```

## API Endpoints

```mermaid
graph LR
    API_SVC["API Service :8000"] -->|"GET /positions"| SE_POS["/positions\nRead positions:open + ltp from Redis"]
    API_SVC -->|"GET /pnl"| SE_PNL["/pnl\nRead pnl:realized + budget + timeline"]
    API_SVC -->|"GET /budget"| SE_BUD["/budget\nRead budget:state from Redis"]
    API_SVC -->|"GET /trades"| SE_TRD["/trades\nRead trades:history sorted set"]
```
