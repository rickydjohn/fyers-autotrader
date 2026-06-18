"""
Cross-sectional momentum backtest — the one equity anomaly with decades of
out-of-sample evidence — with the realism filters that decide whether it's tradeable:

  * LIQUIDITY FLOOR (as-of each rebalance, no lookahead): only rank names whose
    trailing-20d turnover clears `min_turnover_cr`. Momentum's edge concentrates in
    small/illiquid names; restricting to the tradeable set is the honest test.
  * REALISTIC COST: `cost_roundtrip` charged on the turned-over basket fraction.
  * REGIME GATE: hold the basket only when the market (`regime_symbol`) is above its
    `regime_ema`; otherwise sit in cash. Momentum's known failure mode is sharp
    market reversals — the gate is the evidence-backed defense.

Benchmark = always-invested equal-weight (liquid) universe, so the reported alpha is
strategy-over-passive. Returns are monthly; the long-only top-quintile net return is
the realizable number (cash equity can't harvest the short leg).
"""

import bisect
import logging
from dataclasses import dataclass
from statistics import mean

from data import CandleProvider
from features.indicators import ema_series
from models import EquitySymbol

logger = logging.getLogger(__name__)


@dataclass
class _Series:
    dates: list    # sorted trading dates
    closes: list
    volumes: list


def _load_series(symbols: list[EquitySymbol], provider: CandleProvider, history: int) -> dict[str, _Series]:
    out: dict[str, _Series] = {}
    for idx, sym in enumerate(symbols, 1):
        bars = provider.daily_bars(sym.symbol, limit=history)
        if len(bars) < 300:
            continue
        out[sym.symbol] = _Series(
            [b.timestamp.date() for b in bars],
            [b.close for b in bars],
            [b.volume for b in bars],
        )
        if idx % 200 == 0:
            logger.info("…loaded %d/%d symbols", idx, len(symbols))
    return out


def _rebalance_dates(series: dict[str, _Series]) -> list:
    all_dates = set()
    for s in series.values():
        all_dates.update(s.dates)
    by_month = {}
    for d in sorted(all_dates):
        by_month[(d.year, d.month)] = d
    return [by_month[k] for k in sorted(by_month)]


def _pos_on_or_before(s: _Series, d) -> int:
    return bisect.bisect_right(s.dates, d) - 1


def _cum(rets: list[float]) -> float:
    v = 1.0
    for r in rets:
        v *= (1.0 + r)
    return v


def _regime_checker(provider, regime_symbol, regime_ema, history):
    """Returns (fn(date)->bool, label). fn is True when market is above its EMA."""
    if not regime_symbol:
        return (lambda d: True), "none"
    bars = provider.daily_bars(regime_symbol, limit=history)
    if len(bars) <= regime_ema:
        logger.warning("regime symbol %s: insufficient history, gate disabled", regime_symbol)
        return (lambda d: True), "unavailable"
    dates = [b.timestamp.date() for b in bars]
    closes = [b.close for b in bars]
    ema = ema_series(closes, regime_ema)

    def on(d):
        p = bisect.bisect_right(dates, d) - 1
        if p < regime_ema:
            return True                       # not enough EMA history yet → don't gate
        return closes[p] > ema[p]

    return on, f"{regime_symbol} > {regime_ema}EMA"


def run_momentum_backtest(
    symbols: list[EquitySymbol],
    provider: CandleProvider,
    history: int = 900,
    lookback: int = 252,
    skip: int = 21,
    quantile: float = 0.20,
    min_names: int = 20,
    cost_roundtrip: float = 0.0035,
    min_turnover_cr: float = 0.0,
    regime_symbol: str | None = None,
    regime_ema: int = 200,
) -> str:
    series = _load_series(symbols, provider, history)
    if len(series) < min_names:
        return f"Too few symbols with usable history ({len(series)} < {min_names})."

    rebal = _rebalance_dates(series)
    regime_on, regime_label = _regime_checker(provider, regime_symbol, regime_ema, history)

    records = []
    for k in range(len(rebal) - 1):
        d, d_next = rebal[k], rebal[k + 1]
        scored = []  # (momentum, forward_return, symbol) among LIQUID eligible names
        for sym, s in series.items():
            i = _pos_on_or_before(s, d)
            j = _pos_on_or_before(s, d_next)
            if i < lookback + skip or j <= i:
                continue
            if min_turnover_cr > 0:                       # liquidity floor as-of d
                w0 = max(0, i - 20)
                turn_cr = mean(s.closes[t] * s.volumes[t] for t in range(w0, i + 1)) / 1e7
                if turn_cr < min_turnover_cr:
                    continue
            base = s.closes[i - skip - lookback]
            if base <= 0 or s.closes[i] <= 0:
                continue
            scored.append((s.closes[i - skip] / base - 1.0, s.closes[j] / s.closes[i] - 1.0, sym))

        if len(scored) < min_names:
            continue
        uni = mean(f for _, f, _ in scored)               # always-invested benchmark
        on = regime_on(d)
        if on:
            scored.sort(key=lambda x: x[0], reverse=True)
            n = max(1, int(len(scored) * quantile))
            top, bot = scored[:n], scored[-n:]
            records.append({"year": d.year, "regime": "on", "uni": uni,
                            "top_set": {s for _, _, s in top},
                            "top": mean(f for _, f, _ in top),
                            "bot": mean(f for _, f, _ in bot)})
        else:
            records.append({"year": d.year, "regime": "off", "uni": uni,
                            "top_set": set(), "top": 0.0, "bot": 0.0})

    if not records:
        return "No rebalance periods with enough names."

    # Net-of-cost strategy return. Buy cost on newly-added names; sell cost when going to cash.
    prev: set = set()
    for r in records:
        cur = r["top_set"]
        if cur:
            buy_frac = len(cur - prev) / len(cur)
            r["net"] = r["top"] - buy_frac * cost_roundtrip
            r["turnover"] = buy_frac
        else:
            r["net"] = -(0.5 * cost_roundtrip if prev else 0.0)   # sell the book into cash
            r["turnover"] = 0.0
        prev = cur

    periods = len(records)
    off_n = sum(1 for r in records if r["regime"] == "off")
    g_top = mean(r["top"] for r in records if r["regime"] == "on") if off_n < periods else 0.0
    n_top = mean(r["net"] for r in records)
    uni = mean(r["uni"] for r in records)
    avg_turn = mean(r["turnover"] for r in records)

    def mo(x):
        return f"{x*100:>+6.2f}%/mo ({x*12*100:>+5.1f}%/yr)"

    lines = [
        "",
        "=" * 100,
        f"MOMENTUM (TRADEABLE TEST)  {len(series)} symbols, {periods} rebalances, top {quantile*100:.0f}%",
        f"  filters: liquidity ≥ ₹{min_turnover_cr:.0f}cr/day · cost {cost_roundtrip*100:.2f}% round-trip "
        f"· regime gate [{regime_label}] off in {off_n}/{periods} mo",
        "-" * 100,
        f"  STRATEGY (regime-gated momentum, net)  {mo(n_top)}   cumulative {_cum([r['net'] for r in records]):.2f}x",
        f"  BENCHMARK (always-invested universe)   {mo(uni)}   cumulative {_cum([r['uni'] for r in records]):.2f}x",
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
