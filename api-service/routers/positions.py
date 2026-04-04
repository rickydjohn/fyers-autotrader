import json
from fastapi import APIRouter, Depends
import redis.asyncio as aioredis

from dependencies import get_redis
from models.schemas import ApiResponse

router = APIRouter(prefix="/positions", tags=["Positions"])


@router.get("")
async def get_positions(redis_client: aioredis.Redis = Depends(get_redis)):
    raw = await redis_client.hgetall("positions:open")
    positions = []
    total_invested = 0.0
    for symbol, data in raw.items():
        try:
            pos = json.loads(data)
            # Enrich with current price — use option LTP if an option is held
            option_sym = pos.get("option_symbol")
            price_key = f"market:{option_sym}" if option_sym else f"market:{symbol}"
            market_raw = await redis_client.get(price_key)
            if market_raw:
                market = json.loads(market_raw)
                ltp = market.get("ltp", pos["avg_price"])
                qty = pos["quantity"]
                avg = pos["avg_price"]
                if pos["side"] == "BUY":
                    unrealized_pnl = (ltp - avg) * qty
                else:
                    unrealized_pnl = (avg - ltp) * qty
                pos["ltp"] = ltp
                pos["unrealized_pnl"] = round(unrealized_pnl, 2)
                pos["unrealized_pnl_pct"] = round(unrealized_pnl / (avg * qty) * 100, 3)
            total_invested += pos["avg_price"] * pos.get("quantity", 1)
            positions.append(pos)
        except Exception:
            pass

    return ApiResponse.ok({
        "positions": positions,
        "summary": {
            "total_positions": len(positions),
            "total_invested": round(total_invested, 2),
        },
    })
