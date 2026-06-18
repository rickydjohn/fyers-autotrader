"""
Cross-sectional momentum backtest — the one equity anomaly with decades of
out-of-sample evidence.

Unlike the per-symbol harness (engine.py), this is a PORTFOLIO test: at each monthly
rebalance, rank the whole universe by trailing return and hold the top slice.

  * momentum score = "12-1" return: return over `lookback` (~252d) trading days
    ending `skip` (~21d) days ago. Skipping the most recent month avoids the
    well-known short-term reversal that contaminates raw 12-month return.
  * hold the top quantile (default top 20%), equal-weight, until the next rebalance.
  * the edge test is the LONG-SHORT SPREAD: top-quantile forward return minus
    bottom-quantile. A consistently positive spread = momentum edge in this universe.

Gross of costs — the spread is the honest first read on whether the anomaly is here.
"""

import bisect
import logging
from dataclasses import dataclass
from statistics import mean

from data import CandleProvider
from models import EquitySymbol

logger = logging.getLogger(__name__)


@dataclass
class _Series:
    dates: list   # sorted trading dates
    closes: list  # aligned closes


def _load_series(symbols: list[EquitySymbol], provider: CandleProvider, history: int) -> dict[str, _Series]:
    out: dict[str, _Series] = {}
    for idx, sym in enumerate(symbols, 1):
        bars = provider.daily_bars(sym.symbol, limit=history)
        if len(bars) < 300:           # need ~14 months before the first rebalance
            continue
        out[sym.symbol] = _Series([b.timestamp.date() for b in bars], [b.close for b in bars])
        if idx % 100 == 0:
            logger.info("…loaded %d/%d symbols", idx, len(symbols))
    return out


def _rebalance_dates(series: dict[str, _Series]) -> list:
    """Last trading day of each month across the union of all symbols' dates."""
    all_dates = set()
    for s in series.values():
        all_dates.update(s.dates)
    by_month = {}
    for d in sorted(all_dates):
        by_month[(d.year, d.month)] = d
    return [by_month[k] for k in sorted(by_month)]


def _pos_on_or_before(s: _Series, d) -> int:
    """Index of the last bar with date ≤ d (−1 if none)."""
    return bisect.bisect_right(s.dates, d) - 1


def _cum(rets: list[float]) -> float:
    v = 1.0
    for r in rets:
        v *= (1.0 + r)
    return v


def run_momentum_backtest(
    symbols: list[EquitySymbol],
    provider: CandleProvider,
    history: int = 900,
    lookback: int = 252,
    skip: int = 21,
    quantile: float = 0.20,
    min_names: int = 20,
    cost_roundtrip: float = 0.0035,
) -> str:
    """Backtest + validation. Reports GROSS vs NET-of-cost top-quintile (the tradeable
    long-only leg), turnover, the long-short spread, and a per-calendar-year breakdown
    to expose regime dependence. `cost_roundtrip` is charged on the turned-over fraction
    of the basket at each rebalance (held names incur no cost)."""
    series = _load_series(symbols, provider, history)
    if len(series) < min_names:
        return f"Too few symbols with usable history ({len(series)} < {min_names})."

    rebal = _rebalance_dates(series)
    records = []  # one dict per rebalance period

    for k in range(len(rebal) - 1):
        d, d_next = rebal[k], rebal[k + 1]
        scored = []  # (momentum, forward_return, symbol)
        for sym, s in series.items():
            i = _pos_on_or_before(s, d)
            j = _pos_on_or_before(s, d_next)
            if i < lookback + skip or j <= i:
                continue
            base = s.closes[i - skip - lookback]
            if base <= 0 or s.closes[i] <= 0:
                continue
            mom = s.closes[i - skip] / base - 1.0
            fwd = s.closes[j] / s.closes[i] - 1.0     # buy at d close, sell at d_next close
            scored.append((mom, fwd, sym))

        if len(scored) < min_names:
            continue
        scored.sort(key=lambda x: x[0], reverse=True)
        n = max(1, int(len(scored) * quantile))
        top, bot = scored[:n], scored[-n:]
        records.append({
            "year": d.year,
            "top_set": {sym for _, _, sym in top},
            "top": mean(f for _, f, _ in top),
            "all": mean(f for _, f, _ in scored),
            "bot": mean(f for _, f, _ in bot),
        })

    if not records:
        return "No rebalance periods with enough names — need more history/symbols."

    # Turnover + net-of-cost top-quintile return (cost on the fraction newly bought).
    prev: set = set()
    for i, r in enumerate(records):
        cur = r["top_set"]
        r["turnover"] = 1.0 if i == 0 else len(cur - prev) / len(cur)
        r["net"] = r["top"] - r["turnover"] * cost_roundtrip
        prev = cur

    periods = len(records)
    g_top = mean(r["top"] for r in records)
    n_top = mean(r["net"] for r in records)
    uni = mean(r["all"] for r in records)
    spread = mean(r["top"] - r["bot"] for r in records)
    avg_turn = mean(r["turnover"] for r in records)
    spread_hit = sum(1 for r in records if r["top"] - r["bot"] > 0) / periods * 100

    def mo(x):
        return f"{x*100:>+6.2f}%/mo ({x*12*100:>+5.1f}%/yr)"

    lines = [
        "",
        "=" * 100,
        f"MOMENTUM VALIDATION  ({len(series)} symbols, {periods} rebalances, "
        f"{lookback}-{skip} mom, top {quantile*100:.0f}%, cost {cost_roundtrip*100:.2f}% round-trip)",
        "-" * 100,
        f"  GROSS  top {mo(g_top)}   universe {mo(uni)}   long-short {mo(spread)}  (>0 in {spread_hit:.0f}% of mo)",
        f"  COSTS  avg turnover {avg_turn*100:.0f}%/rebalance",
        f"  NET    top {mo(n_top)}   cumulative {_cum([r['net'] for r in records]):.2f}x"
        f"  vs universe {_cum([r['all'] for r in records]):.2f}x",
        f"  >>> realizable LONG-ONLY alpha (net top − universe): {mo(n_top - uni)} <<<",
        "-" * 100,
        "  by calendar year (long-only, net of cost):",
    ]
    for y in sorted({r["year"] for r in records}):
        rs = [r for r in records if r["year"] == y]
        nt = mean(r["net"] for r in rs)
        ua = mean(r["all"] for r in rs)
        lines.append(f"    {y}  ({len(rs):>2}mo)   top_net {nt*100:>+6.2f}%/mo   "
                     f"universe {ua*100:>+6.2f}%/mo   alpha {(nt-ua)*100:>+6.2f}%/mo")
    lines.append("=" * 100)
    return "\n".join(lines)
