"""
EOD scan entrypoint.

    universe → screen → ranked watchlist (entry / stop / target / R:R)

Run inside the deployed stack (talks to core-engine for candles):
    python scan.py
"""

import logging

from config import settings
from data import get_provider
from models import Candidate
from screener import screen
from universe import load_universe

logger = logging.getLogger(__name__)


def format_watchlist(candidates: list[Candidate], top_n: int = 25) -> str:
    rows = [
        f"{'SYMBOL':<22} {'BUCKET':<11} {'SCORE':>5}  {'ENTRY':>9} {'STOP':>9} "
        f"{'TARGET':>9} {'R:R':>4} {'QTY':>6}  STRATEGIES",
        "-" * 110,
    ]
    for c in candidates[:top_n]:
        p = c.plan
        strats = ",".join(s.strategy for s in c.signals)
        rows.append(
            f"{c.symbol:<22} {c.setup_type.value:<11} {c.rank_score:>5.2f}  "
            f"{p.entry:>9.2f} {p.stop:>9.2f} {p.target:>9.2f} {p.risk_reward:>4.1f} "
            f"{p.quantity:>6}  {strats}"
        )
    return "\n".join(rows)


def run_scan(top_n: int = 25) -> list[Candidate]:
    universe = load_universe()
    logger.info("Scanning %d symbols…", len(universe))
    candidates = screen(universe, get_provider())
    print(f"\n=== EOD WATCHLIST — {len(candidates)} candidates ===\n")
    print(format_watchlist(candidates, top_n))
    return candidates


if __name__ == "__main__":
    logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s: %(message)s")
    run_scan()
