"""
Equity-engine backtest runner.

Replays daily history through the live features→signals→risk pipeline and reports
expectancy in R.

REAL data (inside the deployed stack, talks to core-engine /fyers/history):
    cd equity-engine
    PYTHONPATH=. python ../tests/backtests/backtest_equity_pipeline.py --limit 300 --history 750

SYNTHETIC sanity check (no network) — edge-free random walks should print ≈ 0.0R
expectancy; a strongly positive number would mean lookahead bias in the harness:
    cd equity-engine
    FYERS_CLIENT_ID=x FYERS_SECRET_KEY=x PYTHONPATH=. \
        python ../tests/backtests/backtest_equity_pipeline.py --synthetic
"""

import argparse
import logging
import os
import random
import sys
from datetime import datetime, timedelta

import pytz

EQUITY_ENGINE = os.path.join(os.path.dirname(__file__), "..", "..", "equity-engine")
sys.path.insert(0, os.path.abspath(EQUITY_ENGINE))
os.environ.setdefault("FYERS_CLIENT_ID", "x")
os.environ.setdefault("FYERS_SECRET_KEY", "x")

from backtest import run_backtest, run_momentum_backtest, summarize  # noqa: E402
from models import Bar, EquitySymbol  # noqa: E402

IST = pytz.timezone("Asia/Kolkata")
_START = datetime(2022, 1, 1, tzinfo=IST)


# ── synthetic edge-free data (random walk) ────────────────────────────────────
def _gen_walk(seed: int, n: int = 750, start: float = 500.0, drift: float = 0.0, vol: float = 0.015) -> list[Bar]:
    random.seed(seed)
    closes = [start]
    for _ in range(n - 1):
        closes.append(max(5.0, closes[-1] * (1.0 + drift + random.gauss(0.0, vol))))
    bars = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i > 0 else c
        span = c * vol * 0.5
        h = max(o, c) + abs(random.gauss(0.0, span))
        l = min(o, c) - abs(random.gauss(0.0, span))
        bars.append(Bar(timestamp=_START + timedelta(days=i), open=round(o, 2),
                        high=round(h, 2), low=round(l, 2), close=round(c, 2), volume=300_000))
    return bars


class SyntheticProvider:
    def __init__(self, data: dict[str, list[Bar]]):
        self._data = data

    def daily_bars(self, symbol: str, limit: int = 750) -> list[Bar]:
        return self._data.get(symbol, [])[-limit:]

    def quote(self, symbol: str):
        bars = self._data.get(symbol)
        return {"ltp": bars[-1].close} if bars else None


def _synthetic_universe(k: int = 30):
    data, syms = {}, []
    for s in range(k):
        sym = f"NSE:SYN{s:02d}-EQ"
        # mix of mild up/flat/down drifts — but all driftless enough to be edge-free
        drift = (s % 5 - 2) * 0.0003
        data[sym] = _gen_walk(seed=1000 + s, drift=drift)
        syms.append(EquitySymbol(symbol=sym, short_symbol=f"SYN{s:02d}", name=sym))
    return syms, SyntheticProvider(data)


def _synthetic_momentum_universe(k: int = 150, n: int = 1800):
    """Driftless random walks — the no-edge control: a sound momentum harness must
    show a long-short spread of ≈ 0 here (no persistent cross-sectional winners).
    Needs many symbols AND many rebalances, else single-sample noise masquerades as
    edge (the whole trap we're guarding against)."""
    data, syms = {}, []
    for s in range(k):
        sym = f"NSE:MOM{s:03d}-EQ"
        data[sym] = _gen_walk(seed=5000 + s, n=n, drift=0.0)   # drift=0 → no real momentum
        syms.append(EquitySymbol(symbol=sym, short_symbol=f"MOM{s:03d}", name=sym))
    return syms, SyntheticProvider(data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true", help="run on edge-free random data (bias check)")
    ap.add_argument("--momentum", action="store_true", help="synthetic momentum control (expect ~0 spread)")
    ap.add_argument("--limit", type=int, default=200, help="cap universe size (real mode)")
    ap.add_argument("--symbols", type=str, default="", help="comma-separated tickers (real mode)")
    ap.add_argument("--history", type=int, default=750, help="daily bars per symbol")
    ap.add_argument("--no-liquidity", action="store_true", help="skip liquidity filter")
    args = ap.parse_args()
    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s: %(message)s")

    if args.momentum:
        symbols, provider = _synthetic_momentum_universe()
        print(run_momentum_backtest(symbols, provider, history=1800))
        print("\n(driftless control — long-short spread should be ≈ 0%/mo)")
        return

    if args.synthetic:
        symbols, provider = _synthetic_universe()
    else:
        from data import get_provider
        from universe import load_universe
        provider = get_provider()
        if args.symbols:
            symbols = [EquitySymbol(symbol=s, short_symbol=s, name=s) for s in args.symbols.split(",")]
        else:
            symbols = load_universe()[: args.limit]

    print(f"\nBacktesting {len(symbols)} symbols, {args.history} bars each…")
    trades = run_backtest(symbols, provider, history=args.history, apply_liquidity=not args.no_liquidity)
    print(summarize(trades))

    if args.synthetic:
        exp = sum(t.r_multiple for t in trades) / len(trades) if trades else 0.0
        verdict = "OK (no lookahead bias)" if abs(exp) < 0.15 else "⚠ SUSPICIOUS — investigate harness"
        print(f"\nSynthetic edge-free expectancy = {exp:+.3f}R  →  {verdict}")


if __name__ == "__main__":
    main()
