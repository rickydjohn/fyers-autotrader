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


def _mk(sym: str) -> EquitySymbol:
    sym = sym.strip()
    return EquitySymbol(symbol=sym, short_symbol=sym.split(":")[-1].replace("-EQ", ""), name=sym)


def _resolve_symbols(args) -> list[EquitySymbol]:
    path = getattr(args, "symbols_file", "")
    if path:
        with open(path) as f:
            toks = f.read().replace("\n", ",").split(",")
        return [_mk(t) for t in toks if t.strip()]
    if args.symbols:
        return [_mk(s) for s in args.symbols.split(",")]
    from universe import load_universe

    universe = load_universe()
    return universe[: args.limit] if args.limit else universe


from universe import is_etf as _is_etf


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


def cmd_momentum(args):
    from backtest import run_momentum_backtest
    from data import get_provider

    symbols = _resolve_symbols(args)
    print(f"Momentum backtest over {len(symbols)} symbols, {args.history} bars each…")
    print(run_momentum_backtest(
        symbols, get_provider(), history=args.history, quantile=args.quantile,
        cost_roundtrip=args.cost, min_turnover_cr=args.min_turnover, top_liquid=args.top_liquid,
        regime_symbol=None if args.no_regime else args.regime_symbol,
    ))


def cmd_screen_momentum(args):
    from data import get_provider
    from screener import momentum_watchlist

    symbols = _resolve_symbols(args)
    rows = momentum_watchlist(symbols, get_provider(), top_n=args.top,
                              min_turnover_cr=args.min_turnover, clean_only=args.clean)
    print(f"\n=== MOMENTUM WATCHLIST ({'clean' if args.clean else 'raw'}, top {len(rows)}) ===\n")
    print(f"{'#':>3} {'SYMBOL':<18} {'LTP':>9} {'MOM12m':>7} {'PCTL':>4} {'REGIME':<9} "
          f"{'52WH%':>6} {'CPR':<11} {'RSI':>4} {'ENTRY/STOP/TARGET':>26}")
    for r in rows:
        print(f"{r['rank']:>3} {r['name']:<18} {r['ltp']:>9.1f} {r['momentum_12_1_pct']:>6.0f}% "
              f"{r['momentum_pctile']:>4} {r['regime']:<9} {r['pct_from_52w_high']:>6.0f} "
              f"{r['monthly_cpr']:<11} {r['rsi']:>4.0f} "
              f"{r['ref_entry']:>8.1f}/{r['ref_stop']:.1f}/{r['ref_target']:.1f}")


def cmd_analyze(args):
    import json as _json
    from analysis import run_analysis
    from data import get_provider
    from screener import momentum_watchlist

    provider = get_provider()
    universe = _resolve_symbols(args)
    rows = momentum_watchlist(universe, provider, top_n=args.candidates, clean_only=True) if args.candidates else []
    candidates = [_mk(r["symbol"]) for r in rows]
    result = run_analysis(provider, candidates, holdings_limit=args.holdings_limit)

    for section in ("holdings", "candidates"):
        cards = result[section]
        print(f"\n========== {section.upper()} ({len(cards)}) ==========")
        for c in cards:
            rec = c["recommendation"]
            print(f"\n  {c['name']:<14} ₹{c['ltp']:<9} {c['regime']:<9} "
                  f"mom {c['momentum_12m_pct']:+.0f}%  {c['pct_from_52w_high']:+.0f}% from 52wH")
            print(f"    resistance {c['resistances']}  support {c['supports']}")
            if "position" in c:
                p = c["position"]
                print(f"    holding: {p['qty']} @ ₹{p['cost']}  P&L ₹{p['pl']:+.0f} ({p['pl_pct']:+.1f}%) — {p['loss_note']}")
            print(f"    >> {rec.get('action')} ({rec.get('conviction','?')}): {rec.get('reasons','')}")


def cmd_liquid_universe(args):
    """Print the N most-liquid tickers (by recent turnover, from cached daily bars)
    as a comma list — used to self-select a tradeable universe for backtests."""
    from data import get_provider
    from universe import load_universe

    p = get_provider()
    rows = []
    for s in load_universe():
        if _is_etf(s.short_symbol):
            continue
        bars = p.daily_bars(s.symbol, limit=150)   # shallow fetch — enough for turnover
        if len(bars) < 120:
            continue
        recent = bars[-60:]
        turnover_cr = sum(b.close * b.volume for b in recent) / len(recent) / 1e7
        rows.append((turnover_cr, s.symbol))
    rows.sort(reverse=True)
    print(",".join(sym for _, sym in rows[: args.top]))


def cmd_multifactor(args):
    from backtest import run_multifactor_backtest
    from data import get_provider

    symbols = _resolve_symbols(args)
    print(f"Multi-factor backtest over {len(symbols)} symbols, {args.history} bars each…")
    print(run_multifactor_backtest(
        symbols, get_provider(), history=args.history, quantile=args.quantile,
        cost_roundtrip=args.cost, min_turnover_cr=args.min_turnover, top_liquid=args.top_liquid,
        w_mom=args.w_mom, w_lowvol=args.w_lowvol, w_rev=args.w_rev,
        regime_symbol=None if args.no_regime else args.regime_symbol,
    ))


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

    pm = sub.add_parser("momentum", help="cross-sectional momentum backtest")
    pm.add_argument("--limit", type=int, default=0, help="cap universe size (0 = full)")
    pm.add_argument("--symbols", type=str, default="", help="comma-separated tickers")
    pm.add_argument("--symbols-file", type=str, default="", help="path to a file of tickers")
    pm.add_argument("--history", type=int, default=900, help="daily bars per symbol")
    pm.add_argument("--quantile", type=float, default=0.20, help="top/bottom fraction")
    pm.add_argument("--cost", type=float, default=0.0035, help="round-trip cost fraction (0.0035 = 0.35%)")
    pm.add_argument("--min-turnover", type=float, default=0.0, help="liquidity floor, ₹ crore/day (0 = off)")
    pm.add_argument("--top-liquid", type=int, default=0, help="keep only the N most-liquid names as-of each rebalance (0 = off)")
    pm.add_argument("--no-regime", action="store_true", help="disable the market regime gate")
    pm.add_argument("--regime-symbol", default="NSE:NIFTY50-INDEX", help="market index for the regime gate")
    pm.set_defaults(func=cmd_momentum)

    pf = sub.add_parser("multifactor", help="multi-factor composite backtest (momentum + low-vol + reversal)")
    pf.add_argument("--limit", type=int, default=0)
    pf.add_argument("--symbols", type=str, default="")
    pf.add_argument("--symbols-file", type=str, default="", help="path to a file of tickers")
    pf.add_argument("--history", type=int, default=3500, help="daily bars per symbol (~14yr at 3500)")
    pf.add_argument("--quantile", type=float, default=0.20)
    pf.add_argument("--cost", type=float, default=0.0035)
    pf.add_argument("--min-turnover", type=float, default=0.0)
    pf.add_argument("--top-liquid", type=int, default=0)
    pf.add_argument("--w-mom", type=float, default=1.0)
    pf.add_argument("--w-lowvol", type=float, default=1.0)
    pf.add_argument("--w-rev", type=float, default=0.0)
    pf.add_argument("--no-regime", action="store_true")
    pf.add_argument("--regime-symbol", default="NSE:NIFTY50-INDEX")
    pf.set_defaults(func=cmd_multifactor)

    pu = sub.add_parser("liquid-universe", help="print the N most-liquid tickers (comma list)")
    pu.add_argument("--top", type=int, default=200)
    pu.set_defaults(func=cmd_liquid_universe)

    ps2 = sub.add_parser("screen-momentum", help="momentum-ranked discretionary watchlist")
    ps2.add_argument("--limit", type=int, default=0)
    ps2.add_argument("--symbols", type=str, default="")
    ps2.add_argument("--symbols-file", type=str, default="")
    ps2.add_argument("--top", type=int, default=30)
    ps2.add_argument("--min-turnover", type=float, default=10.0)
    ps2.add_argument("--clean", action="store_true", help="clean-momentum filter (uptrend, not overbought, near high)")
    ps2.set_defaults(func=cmd_screen_momentum)

    pa = sub.add_parser("analyze", help="LLM entry/exit analysis of holdings + candidates")
    pa.add_argument("--limit", type=int, default=0)
    pa.add_argument("--symbols", type=str, default="")
    pa.add_argument("--symbols-file", type=str, default="", help="universe to screen for candidates")
    pa.add_argument("--candidates", type=int, default=5, help="top-N momentum candidates to analyse (0 = none)")
    pa.add_argument("--holdings-limit", type=int, default=0, help="cap holdings analysed (0 = all)")
    pa.set_defaults(func=cmd_analyze)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
