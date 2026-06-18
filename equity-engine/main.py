"""
equity-engine service daemon.

Minimal for now: keeps the container alive and exposes health + an on-demand scan.
Full orchestration (EOD scheduler + intraday monitor) is task #7. The backtest is run
on demand via the CLI (``docker exec trading-equity python cli.py backtest …``).
"""

import asyncio
import logging

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from config import settings
from dashboard import PAGE

logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="equity-engine")


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "trading_mode": settings.trading_mode}


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Self-contained equity dashboard (Potential stocks + My holdings, mode + trade)."""
    return PAGE


@app.get("/screener/momentum")
async def screener_momentum(top_n: int = 30, min_turnover_cr: float = 10.0, clean: bool = False):
    """Momentum-ranked discretionary watchlist over the liquid NSE universe.
    Blocking + slow (fetches daily bars), so it runs in a worker thread."""
    from data import get_provider
    from screener import momentum_watchlist
    from universe import load_universe

    def _run():
        return momentum_watchlist(load_universe(), get_provider(), top_n=top_n,
                                  min_turnover_cr=min_turnover_cr, clean_only=clean)

    rows = await asyncio.to_thread(_run)
    return {"status": "ok", "count": len(rows), "watchlist": rows}


@app.get("/mode")
async def get_mode():
    from execution import store
    return {"mode": await asyncio.to_thread(store.get_mode)}


@app.post("/mode")
async def set_mode(mode: str):
    """Switch operating mode: 'simulation' (paper) or 'live' (real orders)."""
    from execution import store
    return {"mode": await asyncio.to_thread(store.set_mode, mode)}


@app.get("/positions")
async def positions():
    """Held stocks for the UI: real Fyers holdings (ACTUAL) + paper positions (PAPER),
    each with buy price, qty and P&L."""
    from execution import list_positions, store
    rows = await asyncio.to_thread(list_positions)
    mode = await asyncio.to_thread(store.get_mode)
    return {"status": "ok", "mode": mode, "count": len(rows), "positions": rows}


@app.post("/trade")
async def trade(symbol: str, side: str, qty: int, confirm: bool = False):
    """Buy/sell, routed by mode. Simulation → paper fill at LTP. Live → real Fyers CNC
    order, but only when confirm=true (else returns confirm_required)."""
    from execution import execute
    return await asyncio.to_thread(execute, symbol, side, qty, confirm)


@app.post("/analysis/run")
async def analysis_run(candidates: int = 8, clean: bool = True):
    """On-demand LLM entry/exit report: every holding (exit advice + P&L context) +
    the top momentum candidates (entry advice). Slow (per-stock LLM calls) → threaded."""
    from analysis import run_analysis
    from data import get_provider
    from models import EquitySymbol
    from screener import momentum_watchlist
    from universe import load_universe

    def _run():
        provider = get_provider()
        rows = momentum_watchlist(load_universe(), provider, top_n=candidates, clean_only=clean) if candidates else []
        cand_syms = [EquitySymbol(symbol=r["symbol"], short_symbol=r["name"], name=r["name"]) for r in rows]
        return run_analysis(provider, cand_syms)

    result = await asyncio.to_thread(_run)
    return {"status": "ok", **result}


@app.post("/scan/run")
async def scan_run(top_n: int = 25):
    """Run the EOD universe scan and return the ranked watchlist. Blocking + slow
    (fetches daily bars for the universe), so it runs in a worker thread."""
    from scan import run_scan

    candidates = await asyncio.to_thread(run_scan, top_n)
    return {
        "status": "ok",
        "count": len(candidates),
        "watchlist": [
            {
                "symbol": c.symbol,
                "setup_type": c.setup_type.value,
                "rank_score": c.rank_score,
                "plan": c.plan.model_dump() if c.plan else None,
                "strategies": [s.strategy for s in c.signals],
            }
            for c in candidates[:top_n]
        ],
    }
