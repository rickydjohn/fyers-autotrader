"""
Historical data proxy router — forwards requests to data-service.
Endpoints:
  GET /api/v1/historical-data
  GET /api/v1/aggregated-view
  GET /api/v1/context-snapshot
  GET /api/v1/decision-history
  GET /api/v1/trade-history
  GET /api/v1/pnl-summary
  GET /api/v1/daily-indicators
  POST /api/v1/historical-backfill  (proxies to core-engine — Fyers → data-service)
"""

from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Query, HTTPException, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["Historical & Context"])


def _get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client


@router.get("/historical-data")
async def historical_data(
    symbol:   str = Query(...),
    interval: str = Query("5m"),
    limit:    int = Query(200),
    since:    Optional[str] = Query(None),
    client: httpx.AsyncClient = Depends(_get_http_client),
):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if since:
        params["since"] = since
    try:
        r = await client.get("/api/v1/historical-data", params=params)
        return JSONResponse(content=r.json(), status_code=r.status_code)
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"data-service unavailable: {e}")


@router.get("/aggregated-view")
async def aggregated_view(
    symbol:   str = Query(...),
    interval: str = Query("1h"),
    limit:    int = Query(100),
    client: httpx.AsyncClient = Depends(_get_http_client),
):
    try:
        r = await client.get(
            "/api/v1/aggregated-view",
            params={"symbol": symbol, "interval": interval, "limit": limit},
        )
        return JSONResponse(content=r.json(), status_code=r.status_code)
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"data-service unavailable: {e}")


@router.get("/context-snapshot")
async def context_snapshot(
    symbol: str = Query(...),
    fresh:  bool = Query(False),
    client: httpx.AsyncClient = Depends(_get_http_client),
):
    try:
        r = await client.get(
            "/api/v1/context-snapshot",
            params={"symbol": symbol, "fresh": str(fresh).lower()},
        )
        return JSONResponse(content=r.json(), status_code=r.status_code)
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"data-service unavailable: {e}")


@router.get("/decision-history")
async def decision_history(
    symbol:        Optional[str] = Query(None),
    limit:         int           = Query(100),
    since:         Optional[str] = Query(None),
    decision_type: Optional[str] = Query(None),
    client: httpx.AsyncClient = Depends(_get_http_client),
):
    params: dict = {"limit": limit}
    if symbol:       params["symbol"] = symbol
    if since:        params["since"] = since
    if decision_type: params["decision_type"] = decision_type
    try:
        r = await client.get("/api/v1/decision-history", params=params)
        return JSONResponse(content=r.json(), status_code=r.status_code)
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"data-service unavailable: {e}")


@router.get("/trade-history")
async def trade_history(
    symbol: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit:  int           = Query(100),
    since:  Optional[str] = Query(None),
    client: httpx.AsyncClient = Depends(_get_http_client),
):
    params: dict = {"limit": limit}
    if symbol: params["symbol"] = symbol
    if status: params["status"] = status
    if since:  params["since"] = since
    try:
        r = await client.get("/api/v1/trade-history", params=params)
        return JSONResponse(content=r.json(), status_code=r.status_code)
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"data-service unavailable: {e}")


@router.get("/pnl-summary")
async def pnl_summary_db(
    since:  Optional[str] = Query(None),
    client: httpx.AsyncClient = Depends(_get_http_client),
):
    params = {}
    if since: params["since"] = since
    try:
        r = await client.get("/api/v1/pnl-summary", params=params)
        return JSONResponse(content=r.json(), status_code=r.status_code)
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"data-service unavailable: {e}")


@router.get("/daily-indicators")
async def daily_indicators(
    symbol: str = Query(...),
    days:   int = Query(5),
    client: httpx.AsyncClient = Depends(_get_http_client),
):
    try:
        r = await client.get(
            "/api/v1/daily-indicators",
            params={"symbol": symbol, "days": days},
        )
        return JSONResponse(content=r.json(), status_code=r.status_code)
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"data-service unavailable: {e}")


@router.post("/historical-backfill")
async def historical_backfill(
    request: Request,
    symbols: Optional[str] = Query(None),
):
    """Trigger Fyers → data-service candle backfill (core-engine). Defaults to watchlist symbols."""
    try:
        core = request.app.state.http_core_client
        params = {"symbols": symbols} if symbols else {}
        r = await core.post("/historical/backfill", params=params)
        return JSONResponse(content=r.json(), status_code=r.status_code)
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"core-engine unavailable: {e}")
