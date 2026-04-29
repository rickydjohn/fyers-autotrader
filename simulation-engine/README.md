# simulation-engine

Consumes trading decisions from the Redis stream, manages virtual or live trade execution, enforces exit rules on every position-watcher cycle, and tracks P&L via the budget system.

**Port:** `8002`

## Responsibilities

- Redis stream consumer (`trading_signals`)
- Option strike selection (ATM CE/PE based on LLM decision direction)
- Order placement — simulation (mock broker) or live (Fyers broker)
- Exit rule enforcement: stop-loss, trailing stop, target, PA exits, time-based close
- Position watcher loop (runs every `POSITION_WATCHER_INTERVAL_SECONDS`)
- Budget tracking and trade persistence to data-service

## Key environment variables

| Variable | Default | Description |
|---|---|---|
| `ENVIRONMENT` | `simulation` | `simulation` (paper trading) or `live` (real Fyers orders) |
| `INITIAL_BUDGET` | `100000` | Starting virtual capital (INR) |
| `MAX_POSITION_SIZE_PCT` | `10` | Max % of budget allocated per trade |
| `SLIPPAGE_PCT` | `0.05` | Entry/exit slippage applied in simulation |
| `COMMISSION_FLAT` | `20` | Minimum commission per trade (INR) |
| `MIN_OPTION_PREMIUM` | `30` | Skip strikes where premium is below this (₹) |
| `SL_COOLDOWN_MINUTES` | `15` | Block re-entry on an underlying after a stop-loss |
| `POSITION_WATCHER_INTERVAL_SECONDS` | `5` | How often to check open positions for exits |

See [`EXAMPLE.env`](../EXAMPLE.env) for the full variable list.

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for component map, exit rule logic, and broker abstraction design.
