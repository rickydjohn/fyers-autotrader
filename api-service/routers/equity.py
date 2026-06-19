"""Equity router — proxies the equity-engine (screener, holdings, analysis, execution)
so the browser talks only to api-service (one port)."""
import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/equity", tags=["Equity"])


def _client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_equity_client


async def _proxy(request: Request, method: str, path: str, params: dict | None = None):
    client = _client(request)
    try:
        r = await client.request(method, path, params=params)
        return JSONResponse(content=r.json(), status_code=r.status_code)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/positions")
async def positions(request: Request):
    return await _proxy(request, "GET", "/positions")


@router.get("/analysis/cached")
async def analysis_cached(request: Request):
    return await _proxy(request, "GET", "/analysis/cached")


@router.post("/analysis/refresh")
async def analysis_refresh(request: Request, candidates: int = Query(8)):
    return await _proxy(request, "POST", "/analysis/refresh", {"candidates": candidates})


@router.get("/mode")
async def get_mode(request: Request):
    return await _proxy(request, "GET", "/mode")


@router.post("/mode")
async def set_mode(request: Request, mode: str = Query(...)):
    return await _proxy(request, "POST", "/mode", {"mode": mode})


@router.post("/trade")
async def trade(
    request: Request,
    symbol: str = Query(...),
    side: str = Query(...),
    qty: int = Query(...),
    confirm: bool = Query(False),
):
    return await _proxy(request, "POST", "/trade",
                        {"symbol": symbol, "side": side, "qty": qty, "confirm": confirm})


@router.get("/screener/momentum")
async def screener_momentum(request: Request, top_n: int = Query(30), clean: bool = Query(True)):
    return await _proxy(request, "GET", "/screener/momentum", {"top_n": top_n, "clean": clean})
