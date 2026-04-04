"""
Trading mode and funds proxy — forwards to core-engine.
"""
import httpx
from fastapi import APIRouter, Query, HTTPException, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["Trading Mode"])


def _core(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_core_client


def _sim(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_sim_client


@router.get("/trading-mode")
async def get_trading_mode(request: Request):
    try:
        r = await _core(request).get("/trading-mode")
        return JSONResponse(content=r.json(), status_code=r.status_code)
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"core-engine unavailable: {e}")


@router.post("/trading-mode")
async def set_trading_mode(request: Request, mode: str = Query(...)):
    try:
        r = await _core(request).post("/trading-mode", params={"mode": mode})
        return JSONResponse(content=r.json(), status_code=r.status_code)
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"core-engine unavailable: {e}")


@router.get("/funds")
async def get_funds(request: Request):
    try:
        r = await _core(request).get("/fyers/funds")
        return JSONResponse(content=r.json(), status_code=r.status_code)
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"core-engine unavailable: {e}")


@router.get("/simulation-budget")
async def get_simulation_budget(request: Request):
    try:
        r = await _sim(request).get("/budget")
        return JSONResponse(content=r.json(), status_code=r.status_code)
    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"simulation-engine unavailable: {e}")
