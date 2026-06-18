"""
equity-engine service daemon.

Minimal for now: keeps the container alive and exposes health + an on-demand scan.
Full orchestration (EOD scheduler + intraday monitor) is task #7. The backtest is run
on demand via the CLI (``docker exec trading-equity python cli.py backtest …``).
"""

import asyncio
import logging

from fastapi import FastAPI

from config import settings

logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="equity-engine")


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "trading_mode": settings.trading_mode}


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
