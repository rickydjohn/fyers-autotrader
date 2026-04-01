"""
P&L analytics: computes realized/unrealized P&L, win rate, drawdown.
"""

import json
import logging
from datetime import datetime, timezone
from typing import List

import pytz
import redis.asyncio as aioredis

from models.schemas import PnLSnapshot, Position, Trade

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")


async def get_all_trades(redis_client: aioredis.Redis) -> List[Trade]:
    raw = await redis_client.hgetall("trades:all")
    trades = []
    for _, data in raw.items():
        try:
            trades.append(Trade(**json.loads(data)))
        except Exception:
            pass
    return sorted(trades, key=lambda t: t.entry_time, reverse=True)


async def get_open_positions(redis_client: aioredis.Redis) -> List[Position]:
    raw = await redis_client.hgetall("positions:open")
    positions = []
    for _, data in raw.items():
        try:
            positions.append(Position(**json.loads(data)))
        except Exception:
            pass
    return positions


async def compute_pnl_summary(
    redis_client: aioredis.Redis,
    current_prices: dict,
) -> dict:
    """Compute full P&L summary including unrealized from open positions."""
    trades = await get_all_trades(redis_client)
    positions = await get_open_positions(redis_client)

    closed_trades = [t for t in trades if t.status in ("CLOSED", "STOPPED")]
    realized_pnl = sum(t.pnl or 0 for t in closed_trades)

    # Unrealized from open positions
    unrealized_pnl = 0.0
    for pos in positions:
        ltp = current_prices.get(pos.symbol, pos.avg_price)
        if pos.side == "BUY":
            unrealized_pnl += (ltp - pos.avg_price) * pos.quantity
        else:
            unrealized_pnl += (pos.avg_price - ltp) * pos.quantity

    total_pnl = realized_pnl + unrealized_pnl

    wins = [t for t in closed_trades if (t.pnl or 0) > 0]
    losses = [t for t in closed_trades if (t.pnl or 0) < 0]
    win_rate = len(wins) / len(closed_trades) if closed_trades else 0.0
    avg_win = sum(t.pnl for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t.pnl for t in losses) / len(losses) if losses else 0.0

    # Max drawdown from daily P&L timeline
    budget_raw = await redis_client.get("budget:state")
    initial_budget = 100000.0
    if budget_raw:
        budget_data = json.loads(budget_raw)
        initial_budget = budget_data.get("initial", 100000.0)

    return {
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl / initial_budget * 100, 3),
        "win_rate": round(win_rate, 3),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "total_trades": len(closed_trades),
        "open_positions": len(positions),
    }


async def get_pnl_timeline(redis_client: aioredis.Redis, date_str: str) -> List[dict]:
    """Get cumulative P&L timeline for a specific date."""
    date_key = f"pnl:daily:{date_str}"
    raw = await redis_client.zrange(date_key, 0, -1, withscores=False)
    timeline = []
    for item in raw:
        try:
            timeline.append(json.loads(item))
        except Exception:
            pass
    return timeline
