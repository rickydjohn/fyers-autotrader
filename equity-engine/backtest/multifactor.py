"""
Multi-factor composite backtest — the "combine weak uncorrelated signals" approach.

Factors (all PRICE-derived → point-in-time clean, no fundamentals, no lookahead):
  * MOMENTUM    — 12-1 trailing return (higher = better)
  * LOW-VOL     — trailing 1y volatility of daily returns (lower = better; the
                  documented low-volatility premium)
  * REVERSAL    — last-month return (lower = better; short-term mean reversion)

Each factor is converted to a cross-sectional percentile rank at every rebalance, then
combined with configurable weights into one composite. Long the top quintile by
composite, monthly. Same realism filters as the momentum test (liquidity floor /
top-liquid universe, realistic cost, optional regime gate) and the same
strategy-vs-always-invested-universe benchmark.

VALUE/QUALITY are deliberately absent: they need point-in-time fundamentals we don't
have, and current-snapshot fundamentals would inject look-ahead + survivorship bias.
"""

import logging
from statistics import mean, pstdev

from data import CandleProvider
from models import EquitySymbol
from backtest.momentum import (
    _cum,
    _load_series,
    _pos_on_or_before,
    _rebalance_dates,
    _regime_checker,
)

logger = logging.getLogger(__name__)


def _ranks(vals: list[float], higher_better: bool) -> list[float]:
    """Cross-sectional percentile ranks in [0,1]; best value → 1.0."""
    n = len(vals)
    if n <= 1:
        return [0.5] * n
    order = sorted(range(n), key=lambda k: vals[k], reverse=higher_better)
    pct = [0.0] * n
    for rank, k in enumerate(order):
        pct[k] = (n - 1 - rank) / (n - 1)
    return pct


def run_multifactor_backtest(
    symbols: list[EquitySymbol],
    provider: CandleProvider,
    history: int = 3500,
    lookback: int = 252,
    skip: int = 21,
    vol_window: int = 252,
    rev_window: int = 21,
    quantile: float = 0.20,
    min_names: int = 20,
    cost_roundtrip: float = 0.0035,
    min_turnover_cr: float = 0.0,
    top_liquid: int = 0,
    w_mom: float = 1.0,
    w_lowvol: float = 1.0,
    w_rev: float = 0.0,
    regime_symbol: str | None = None,
    regime_ema: int = 200,
) -> str:
    series = _load_series(symbols, provider, history)
    if len(series) < min_names:
        return f"Too few symbols with usable history ({len(series)} < {min_names})."

    rebal = _rebalance_dates(series)
    regime_on, regime_label = _regime_checker(provider, regime_symbol, regime_ema, history)
    warmup = max(lookback + skip, vol_window + 1, rev_window + 1)

    records = []
    for k in range(len(rebal) - 1):
        d, d_next = rebal[k], rebal[k + 1]
        cands = []
        for sym, s in series.items():
            i = _pos_on_or_before(s, d)
            j = _pos_on_or_before(s, d_next)
            if i < warmup or j <= i:
                continue
            w0 = max(0, i - 20)
            turn_cr = mean(s.closes[t] * s.volumes[t] for t in range(w0, i + 1)) / 1e7
            if min_turnover_cr > 0 and turn_cr < min_turnover_cr:
                continue
            base = s.closes[i - skip - lookback]
            if base <= 0 or s.closes[i] <= 0 or s.closes[i - rev_window] <= 0:
                continue
            rets = [s.closes[t] / s.closes[t - 1] - 1.0 for t in range(i - vol_window + 1, i + 1)]
            cands.append({
                "sym": sym,
                "mom": s.closes[i - skip] / base - 1.0,
                "vol": pstdev(rets),
                "rev": s.closes[i] / s.closes[i - rev_window] - 1.0,
                "fwd": s.closes[j] / s.closes[i] - 1.0,
                "turn": turn_cr,
            })

        if top_liquid > 0 and len(cands) > top_liquid:
            cands.sort(key=lambda c: c["turn"], reverse=True)
            cands = cands[:top_liquid]
        if len(cands) < min_names:
            continue

        uni = mean(c["fwd"] for c in cands)
        if regime_on(d):
            r_mom = _ranks([c["mom"] for c in cands], higher_better=True)
            r_vol = _ranks([c["vol"] for c in cands], higher_better=False)
            r_rev = _ranks([c["rev"] for c in cands], higher_better=False)
            comp = [w_mom * r_mom[x] + w_lowvol * r_vol[x] + w_rev * r_rev[x] for x in range(len(cands))]
            order = sorted(range(len(cands)), key=lambda x: comp[x], reverse=True)
            n = max(1, int(len(cands) * quantile))
            top_idx, bot_idx = order[:n], order[-n:]
            records.append({"year": d.year, "regime": "on", "uni": uni,
                            "top_set": {cands[x]["sym"] for x in top_idx},
                            "top": mean(cands[x]["fwd"] for x in top_idx),
                            "bot": mean(cands[x]["fwd"] for x in bot_idx)})
        else:
            records.append({"year": d.year, "regime": "off", "uni": uni,
                            "top_set": set(), "top": 0.0, "bot": 0.0})

    if not records:
        return "No rebalance periods with enough names."

    prev: set = set()
    for r in records:
        cur = r["top_set"]
        if cur:
            buy_frac = len(cur - prev) / len(cur)
            r["net"] = r["top"] - buy_frac * cost_roundtrip
            r["turnover"] = buy_frac
        else:
            r["net"] = -(0.5 * cost_roundtrip if prev else 0.0)
            r["turnover"] = 0.0
        prev = cur

    periods = len(records)
    off_n = sum(1 for r in records if r["regime"] == "off")
    n_top = mean(r["net"] for r in records)
    uni = mean(r["uni"] for r in records)
    avg_turn = mean(r["turnover"] for r in records)

    def mo(x):
        return f"{x*100:>+6.2f}%/mo ({x*12*100:>+5.1f}%/yr)"

    universe_desc = (f"top-{top_liquid} most-liquid" if top_liquid
                     else f"liquidity ≥ ₹{int(min_turnover_cr)}cr/day")
    lines = [
        "",
        "=" * 100,
        f"MULTI-FACTOR (mom×{w_mom:g} + lowvol×{w_lowvol:g} + rev×{w_rev:g})  "
        f"{len(series)} symbols, {periods} rebalances, top {quantile*100:.0f}%",
        f"  filters: {universe_desc} · cost {cost_roundtrip*100:.2f}% round-trip "
        f"· regime [{regime_label}] off {off_n}/{periods}mo",
        "-" * 100,
        f"  STRATEGY (net)                        {mo(n_top)}   cumulative {_cum([r['net'] for r in records]):.2f}x",
        f"  BENCHMARK (always-invested universe)  {mo(uni)}   cumulative {_cum([r['uni'] for r in records]):.2f}x",
        f"  >>> ALPHA (strategy − benchmark): {mo(n_top - uni)} <<<   avg turnover {avg_turn*100:.0f}%/rebal",
        "-" * 100,
        "  by calendar year (strategy net vs benchmark):",
    ]
    for y in sorted({r["year"] for r in records}):
        rs = [r for r in records if r["year"] == y]
        nt, ua = mean(r["net"] for r in rs), mean(r["uni"] for r in rs)
        offs = sum(1 for r in rs if r["regime"] == "off")
        lines.append(f"    {y}  ({len(rs):>2}mo, {offs} cash)   strat {nt*100:>+6.2f}%/mo   "
                     f"bench {ua*100:>+6.2f}%/mo   alpha {(nt-ua)*100:>+6.2f}%/mo")
    lines.append("=" * 100)
    return "\n".join(lines)
