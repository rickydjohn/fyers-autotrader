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
    history: int = 750,
    lookback: int = 252,
    skip: int = 21,
    quantile: float = 0.20,
    min_names: int = 20,
) -> str:
    series = _load_series(symbols, provider, history)
    if len(series) < min_names:
        return f"Too few symbols with usable history ({len(series)} < {min_names})."

    rebal = _rebalance_dates(series)
    top_rets, bot_rets, all_rets, spreads = [], [], [], []

    for k in range(len(rebal) - 1):
        d, d_next = rebal[k], rebal[k + 1]
        scored = []  # (momentum, forward_return)
        for s in series.values():
            i = _pos_on_or_before(s, d)
            j = _pos_on_or_before(s, d_next)
            if i < lookback + skip or j <= i:
                continue
            base = s.closes[i - skip - lookback]
            if base <= 0 or s.closes[i] <= 0:
                continue
            mom = s.closes[i - skip] / base - 1.0
            fwd = s.closes[j] / s.closes[i] - 1.0     # buy at d close, sell at d_next close
            scored.append((mom, fwd))

        if len(scored) < min_names:
            continue
        scored.sort(key=lambda x: x[0], reverse=True)
        n = max(1, int(len(scored) * quantile))
        top = [f for _, f in scored[:n]]
        bot = [f for _, f in scored[-n:]]
        top_rets.append(mean(top))
        bot_rets.append(mean(bot))
        all_rets.append(mean(f for _, f in scored))
        spreads.append(mean(top) - mean(bot))

    periods = len(spreads)
    if periods == 0:
        return "No rebalance periods with enough names — need more history/symbols."

    def line(label, rets):
        return (f"  {label:<16} {mean(rets) * 100:>+6.2f}%/mo   "
                f"annualized {mean(rets) * 12 * 100:>+6.1f}%   cumulative {_cum(rets):>5.2f}x")

    spread_hit = sum(1 for s in spreads if s > 0) / periods * 100
    return "\n".join([
        "",
        "=" * 96,
        f"CROSS-SECTIONAL MOMENTUM  ({len(series)} symbols, {periods} monthly rebalances, "
        f"{lookback}-{skip} mom, top/bottom {quantile*100:.0f}%)",
        "-" * 96,
        line("TOP quantile", top_rets),
        line("all universe", all_rets),
        line("BOTTOM quantile", bot_rets),
        "-" * 96,
        line("LONG-SHORT", spreads) + f"   spread>0 in {spread_hit:.0f}% of months",
        "  ^ the edge test — consistently positive long-short spread = momentum edge here",
        "=" * 96,
    ])
