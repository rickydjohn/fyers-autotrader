import json
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, Query
import pytz
import redis.asyncio as aioredis

from dependencies import get_redis
from models.schemas import ApiResponse

router = APIRouter(prefix="/pnl", tags=["P&L"])
IST = pytz.timezone("Asia/Kolkata")


@router.get("")
async def get_pnl(
    period: str = Query("today", pattern="^(today|week|month)$"),
    redis_client: aioredis.Redis = Depends(get_redis),
):
    now = datetime.now(IST)
    date_str = now.strftime("%Y-%m-%d")

    # Realized P&L
    realized_raw = await redis_client.get("pnl:realized:total")
    realized_pnl = float(realized_raw or 0)

    # Unrealized from open positions
    positions_raw = await redis_client.hgetall("positions:open")
    unrealized_pnl = 0.0
    for symbol, pos_data in positions_raw.items():
        try:
            pos = json.loads(pos_data)
            market_raw = await redis_client.get(f"market:{symbol}")
            if market_raw:
                market = json.loads(market_raw)
                ltp = market.get("ltp", pos["avg_price"])
                qty = pos["quantity"]
                avg = pos["avg_price"]
                if pos["side"] == "BUY":
                    unrealized_pnl += (ltp - avg) * qty
                else:
                    unrealized_pnl += (avg - ltp) * qty
        except Exception:
            pass

    # Budget state
    budget_raw = await redis_client.get("budget:state")
    budget = json.loads(budget_raw) if budget_raw else {
        "initial": 100000, "cash": 100000, "invested": 0
    }

    initial = budget.get("initial", 100000)
    total_pnl = realized_pnl + unrealized_pnl

    # Trade stats
    trades_raw = await redis_client.zrevrange("trades:history", 0, -1)
    closed_trades = []
    for item in trades_raw:
        try:
            t = json.loads(item)
            if t.get("status") in ("CLOSED", "STOPPED"):
                closed_trades.append(t)
        except Exception:
            pass

    wins = [t for t in closed_trades if (t.get("pnl") or 0) > 0]
    losses = [t for t in closed_trades if (t.get("pnl") or 0) < 0]

    # Timeline
    timeline_raw = await redis_client.zrange(f"pnl:daily:{date_str}", 0, -1)
    timeline = []
    for item in timeline_raw:
        try:
            timeline.append(json.loads(item))
        except Exception:
            pass

    return ApiResponse.ok({
        "period": period,
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl / initial * 100, 3) if initial else 0,
        "budget": {
            "initial": initial,
            "current": round(initial + total_pnl, 2),
            "cash": round(budget.get("cash", 0), 2),
            "invested": round(budget.get("invested", 0), 2),
            "utilization_pct": round(budget.get("invested", 0) / initial * 100, 2) if initial else 0,
        },
        "win_rate": round(len(wins) / len(closed_trades), 3) if closed_trades else 0,
        "avg_win": round(sum(t.get("pnl", 0) for t in wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(t.get("pnl", 0) for t in losses) / len(losses), 2) if losses else 0,
        "total_trades": len(closed_trades),
        "timeline": timeline,
    })
