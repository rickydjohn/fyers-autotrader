import json
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, Query, Request
import pytz
import redis.asyncio as aioredis

from dependencies import get_redis
from models.schemas import ApiResponse

router = APIRouter(prefix="/pnl", tags=["P&L"])
IST = pytz.timezone("Asia/Kolkata")


async def _live_budget(request: Request) -> Optional[dict]:
    """Fetch real account funds from core-engine (Fyers). Returns None on failure."""
    try:
        r = await request.app.state.http_core_client.get("/fyers/funds")
        if r.status_code != 200:
            return None
        funds = r.json().get("funds", {})
        available = funds.get("available_balance") or funds.get("net_available") or 0.0
        utilized = funds.get("utilized_amount") or funds.get("used_amount") or 0.0
        total = funds.get("total_balance") or (available + utilized) or 0.0
        return {
            "initial": round(total, 2),
            "current": round(available + utilized, 2),
            "cash": round(available, 2),
            "invested": round(utilized, 2),
            "utilization_pct": round(utilized / total * 100, 2) if total else 0.0,
        }
    except Exception:
        return None


@router.get("")
async def get_pnl(
    request: Request,
    period: str = Query("today", pattern="^(today|week|month)$"),
    redis_client: aioredis.Redis = Depends(get_redis),
):
    now = datetime.now(IST)
    date_str = now.strftime("%Y-%m-%d")

    # Trading mode
    mode_raw = await redis_client.get("trading:mode")
    trading_mode = mode_raw if isinstance(mode_raw, str) else (mode_raw.decode() if mode_raw else "simulation")

    # Realized P&L
    realized_raw = await redis_client.get("pnl:realized:total")
    realized_pnl = float(realized_raw or 0)

    # Unrealized from open positions
    positions_raw = await redis_client.hgetall("positions:open")
    unrealized_pnl = 0.0
    for symbol, pos_data in positions_raw.items():
        try:
            pos = json.loads(pos_data)
            # For option positions use the option symbol's market price, not the underlying's
            market_symbol = pos.get("option_symbol") or symbol
            market_raw = await redis_client.get(f"market:{market_symbol}")
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

    total_pnl = realized_pnl + unrealized_pnl

    # Budget — mode-aware
    if trading_mode == "live":
        budget_info = await _live_budget(request)
        if budget_info is None:
            # Fyers unavailable: fall back gracefully with zeros
            budget_info = {"initial": 0, "current": 0, "cash": 0, "invested": 0, "utilization_pct": 0}
        initial = budget_info["initial"]
    else:
        budget_raw = await redis_client.get("budget:state")
        budget = json.loads(budget_raw) if budget_raw else {
            "initial": 100000, "cash": 100000, "invested": 0
        }
        initial = budget.get("initial", 100000)
        budget_info = {
            "initial": initial,
            "current": round(initial + total_pnl, 2),
            "cash": round(budget.get("cash", 0), 2),
            "invested": round(budget.get("invested", 0), 2),
            "utilization_pct": round(budget.get("invested", 0) / initial * 100, 2) if initial else 0,
        }

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
        "budget": budget_info,
        "win_rate": round(len(wins) / len(closed_trades), 3) if closed_trades else 0,
        "avg_win": round(sum(t.get("pnl", 0) for t in wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(t.get("pnl", 0) for t in losses) / len(losses), 2) if losses else 0,
        "total_trades": len(closed_trades),
        "timeline": timeline,
    })
