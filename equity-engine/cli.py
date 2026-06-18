"""
equity-engine CLI — on-demand scan + backtest, for running inside the deployed
container:

    docker exec trading-equity python cli.py scan --top 30
    docker exec trading-equity python cli.py backtest --limit 300 --history 750
    docker exec trading-equity python cli.py backtest --symbols NSE:SBIN-EQ,NSE:TCS-EQ
"""

import argparse
import logging

from config import settings
from models import EquitySymbol


def _resolve_symbols(args) -> list[EquitySymbol]:
    if args.symbols:
        return [
            EquitySymbol(symbol=s.strip(), short_symbol=s.strip().split(":")[-1].replace("-EQ", ""), name=s.strip())
            for s in args.symbols.split(",")
        ]
    from universe import load_universe

    universe = load_universe()
    return universe[: args.limit] if args.limit else universe


def cmd_scan(args):
    from scan import run_scan

    run_scan(top_n=args.top)


def cmd_backtest(args):
    from backtest import run_backtest, summarize
    from data import get_provider

    symbols = _resolve_symbols(args)
    print(f"Backtesting {len(symbols)} symbols, {args.history} bars each…")
    trades = run_backtest(
        symbols, get_provider(), history=args.history, apply_liquidity=not args.no_liquidity
    )
    print(summarize(trades))


def main():
    logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="equity-engine")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("scan", help="run the EOD universe scan")
    ps.add_argument("--top", type=int, default=25)
    ps.set_defaults(func=cmd_scan)

    pb = sub.add_parser("backtest", help="walk-forward backtest on real history")
    pb.add_argument("--limit", type=int, default=0, help="cap universe size (0 = full)")
    pb.add_argument("--symbols", type=str, default="", help="comma-separated tickers")
    pb.add_argument("--history", type=int, default=750, help="daily bars per symbol")
    pb.add_argument("--no-liquidity", action="store_true")
    pb.set_defaults(func=cmd_backtest)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
