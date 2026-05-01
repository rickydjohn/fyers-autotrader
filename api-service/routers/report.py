"""Report router — proxies monthly trade report from data-service."""
from typing import Optional

import httpx
from fastapi import APIRouter, Query, Request, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/report", tags=["Report"])


def _get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client


@router.get("/trades")
async def monthly_report(
    request: Request,
    month: str = Query(..., description="Month in YYYY-MM format, e.g. 2026-04"),
    trading_mode: Optional[str] = Query(None),
):
    client = _get_http_client(request)
    params = {"month": month}
    if trading_mode:
        params["trading_mode"] = trading_mode
    try:
        r = await client.get("/api/v1/report/trades", params=params)
        return JSONResponse(content=r.json(), status_code=r.status_code)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/cumulative")
async def cumulative_report(
    request: Request,
    from_date: Optional[str] = Query(None),
    to_date: Optional[str] = Query(None),
    trading_mode: Optional[str] = Query(None),
):
    client = _get_http_client(request)
    params: dict = {}
    if from_date:
        params["from_date"] = from_date
    if to_date:
        params["to_date"] = to_date
    if trading_mode:
        params["trading_mode"] = trading_mode
    try:
        r = await client.get("/api/v1/report/cumulative", params=params)
        return JSONResponse(content=r.json(), status_code=r.status_code)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
