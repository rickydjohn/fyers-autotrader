import asyncio
import json
from typing import Optional
from fastapi import APIRouter, Depends, Query, Request
import redis.asyncio as aioredis
from sse_starlette.sse import EventSourceResponse

from dependencies import get_redis
from models.schemas import ApiResponse

router = APIRouter(prefix="/decision-log", tags=["Decision Log"])


def _decode_stream_entry(data: dict) -> dict:
    """Redis Streams store all values as strings.
    Decode each field back to its native type via json.loads(), and
    rename 'indicators' → 'indicators_snapshot' to match the Decision schema."""
    result = {}
    for k, v in data.items():
        key = "indicators_snapshot" if k == "indicators" else k
        try:
            result[key] = json.loads(v)
        except (json.JSONDecodeError, TypeError, ValueError):
            result[key] = v
    return result


@router.get("")
async def get_decision_log(
    symbol: Optional[str] = Query(None),
    decision: Optional[str] = Query(None, pattern="^(BUY|SELL|HOLD)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    redis_client: aioredis.Redis = Depends(get_redis),
):
    raw_items = await redis_client.zrevrange("decision:log", offset, offset + limit - 1)
    decisions = []
    for item in raw_items:
        try:
            d = json.loads(item)
            if symbol and d.get("symbol") != symbol:
                continue
            if decision and d.get("decision") != decision:
                continue
            decisions.append(d)
        except Exception:
            pass

    total = await redis_client.zcard("decision:log")
    return ApiResponse.ok({"total": total, "decisions": decisions})


@router.get("/stream")
async def stream_decisions(
    request: Request,
    redis_client: aioredis.Redis = Depends(get_redis),
):
    """SSE endpoint: streams live decisions, trades, P&L updates."""

    async def event_generator():
        last_id = "$"
        heartbeat_interval = 15  # seconds

        while True:
            if await request.is_disconnected():
                break

            try:
                messages = await redis_client.xread(
                    {"decisions": last_id},
                    count=5,
                    block=heartbeat_interval * 1000,
                )

                if not messages:
                    yield {"event": "heartbeat", "data": json.dumps({"ts": "ping"})}
                    continue

                for stream, entries in messages:
                    for entry_id, data in entries:
                        last_id = entry_id
                        yield {
                            "event": "decision",
                            "data": json.dumps(_decode_stream_entry(data)),
                            "id": entry_id,
                        }

            except asyncio.CancelledError:
                break
            except Exception as e:
                yield {"event": "error", "data": json.dumps({"message": str(e)})}
                await asyncio.sleep(2)

    return EventSourceResponse(event_generator())
