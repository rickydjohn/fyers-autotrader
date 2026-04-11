"""Aggregated system health endpoint — queries all internal services concurrently."""
import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Request

router = APIRouter()


async def _probe(client, label: str) -> dict:
    try:
        r = await client.get("/healthz", timeout=2.0)
        if r.status_code == 200:
            data = r.json()
            return {"status": data.get("status", "ok"), "checks": data.get("checks", {})}
        return {"status": "degraded", "checks": {}}
    except Exception as exc:
        return {"status": "unavailable", "checks": {}, "error": str(exc)}


@router.get("/health")
async def system_health(request: Request):
    redis = request.app.state.redis

    try:
        await redis.ping()
        redis_status = "ok"
    except Exception:
        redis_status = "error"

    core_result, sim_result, data_result = await asyncio.gather(
        _probe(request.app.state.http_core_client, "core_engine"),
        _probe(request.app.state.http_sim_client,  "simulation_engine"),
        _probe(request.app.state.http_client,       "data_service"),
    )

    services = {
        "api_service": {
            "status": "ok" if redis_status == "ok" else "degraded",
            "checks": {"redis": redis_status},
        },
        "core_engine":        core_result,
        "simulation_engine":  sim_result,
        "data_service":       data_result,
    }

    all_statuses = [s["status"] for s in services.values()]
    if all(s == "ok" for s in all_statuses):
        overall = "ok"
    elif any(s == "unavailable" or s == "error" for s in all_statuses):
        overall = "degraded"
    else:
        overall = "degraded"

    return {
        "status": overall,
        "services": services,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
