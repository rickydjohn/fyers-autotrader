"""
Walk-forward backtest over daily bars — the SAME features→signals→risk code the
live scanner uses, so a backtest result actually predicts live behaviour.

Per symbol, walking the daily series with no lookahead:
  1. At each bar i, compute features from bars[:i+1] (as-of close of day i).
  2. If a strategy fires and a plan is built, ENTER at the next bar's open.
  3. Walk forward: stop (low ≤ stop) or target (high ≥ target) — stop checked first
     (conservative on same-day touches); time-exit after the bucket's max hold;
     EOD-exit at the last bar. One position per symbol at a time.
  4. Record the trade in R-multiples (exit−entry)/(entry−stop) and %.

Expectancy is reported in R (capital-agnostic), the only honest edge measure: a
positive average-R across many trades is the thing the options system never had.
"""

import logging
from dataclasses import dataclass
from datetime import date
from statistics import mean
from typing import Optional

from data import CandleProvider
from features import build_features
from models import Bar, EquitySymbol, SetupType
from risk import build_plan
from screener.screen import _passes_liquidity
from signals import generate_signals

logger = logging.getLogger(__name__)

WARMUP = 200  # need ~200 bars before EMA200/regime is trustworthy
MAX_HOLD = {SetupType.SWING: 15, SetupType.POSITIONAL: 60}  # trading days


@dataclass
class BTTrade:
    symbol: str
    strategy: str
    setup_type: str
    entry_date: date
    entry: float
    exit_date: date
    exit: float
    exit_reason: str   # TARGET | STOP | TIME | EOD
    r_multiple: float
    pnl_pct: float
    hold_days: int


def _simulate(bars: list[Bar], entry_idx: int, stop: float, target: float, max_hold: int):
    """Return (exit_idx, exit_price, reason). Stop takes priority on same-bar touches."""
    n = len(bars)
    for j in range(entry_idx, n):
        bar = bars[j]
        if bar.low <= stop:
            return j, stop, "STOP"
        if bar.high >= target:
            return j, target, "TARGET"
        if j - entry_idx >= max_hold:
            return j, bar.close, "TIME"
    return n - 1, bars[-1].close, "EOD"


def backtest_symbol(symbol: str, bars: list[Bar], apply_liquidity: bool = True) -> list[BTTrade]:
    trades: list[BTTrade] = []
    n = len(bars)
    i = WARMUP
    while i < n - 1:
        f = build_features(symbol, bars[: i + 1])
        if f is None:
            i += 1
            continue
        if apply_liquidity and not _passes_liquidity(f):
            i += 1
            continue
        signals = generate_signals(f)
        if not signals:
            i += 1
            continue

        best = max(signals, key=lambda s: s.score)
        plan = build_plan(f, best)
        if plan is None:
            i += 1
            continue

        entry_idx = i + 1
        entry = bars[entry_idx].open
        risk = entry - plan.stop
        if risk <= 0:                     # gapped below stop → untradeable
            i += 1
            continue

        exit_idx, exit_price, reason = _simulate(
            bars, entry_idx, plan.stop, plan.target, MAX_HOLD[best.setup_type]
        )
        trades.append(
            BTTrade(
                symbol=symbol,
                strategy=best.strategy,
                setup_type=best.setup_type.value,
                entry_date=bars[entry_idx].timestamp.date(),
                entry=round(entry, 2),
                exit_date=bars[exit_idx].timestamp.date(),
                exit=round(exit_price, 2),
                exit_reason=reason,
                r_multiple=round((exit_price - entry) / risk, 2),
                pnl_pct=round((exit_price - entry) / entry * 100.0, 2),
                hold_days=exit_idx - entry_idx,
            )
        )
        i = exit_idx + 1                  # no overlapping positions per symbol
    return trades


def run_backtest(
    symbols: list[EquitySymbol],
    provider: CandleProvider,
    history: int = 750,
    apply_liquidity: bool = True,
) -> list[BTTrade]:
    all_trades: list[BTTrade] = []
    for idx, sym in enumerate(symbols, 1):
        bars = provider.daily_bars(sym.symbol, limit=history)
        if len(bars) < WARMUP + 5:
            continue
        all_trades.extend(backtest_symbol(sym.symbol, bars, apply_liquidity))
        if idx % 100 == 0:
            logger.info("…%d/%d symbols, %d trades so far", idx, len(symbols), len(all_trades))
    return all_trades


# ── Reporting ─────────────────────────────────────────────────────────────────
def _stats(trades: list[BTTrade]) -> dict:
    n = len(trades)
    wins = [t for t in trades if t.r_multiple > 0]
    losses = [t for t in trades if t.r_multiple <= 0]
    sum_win_r = sum(t.r_multiple for t in wins)
    sum_loss_r = sum(t.r_multiple for t in losses)
    return {
        "trades": n,
        "win_rate": len(wins) / n * 100.0,
        "expectancy_r": mean(t.r_multiple for t in trades),
        "avg_win_r": mean(t.r_multiple for t in wins) if wins else 0.0,
        "avg_loss_r": mean(t.r_multiple for t in losses) if losses else 0.0,
        "profit_factor": (sum_win_r / abs(sum_loss_r)) if sum_loss_r else float("inf"),
        "total_r": sum_win_r + sum_loss_r,
        "avg_hold_days": mean(t.hold_days for t in trades),
        "avg_pnl_pct": mean(t.pnl_pct for t in trades),
    }


def _fmt(label: str, s: dict) -> str:
    return (
        f"{label:<22} n={s['trades']:<5} win={s['win_rate']:>5.1f}%  "
        f"expectancy={s['expectancy_r']:>+5.2f}R  PF={s['profit_factor']:>4.2f}  "
        f"total={s['total_r']:>+7.1f}R  avgWin={s['avg_win_r']:>+4.2f}R avgLoss={s['avg_loss_r']:>+5.2f}R  "
        f"hold={s['avg_hold_days']:>4.1f}d"
    )


def summarize(trades: list[BTTrade]) -> str:
    if not trades:
        return "No trades generated."
    lines = ["", "=" * 120, _fmt("ALL", _stats(trades)), "-" * 120]

    by_setup = {}
    by_strat = {}
    for t in trades:
        by_setup.setdefault(t.setup_type, []).append(t)
        by_strat.setdefault(t.strategy, []).append(t)
    for k in sorted(by_setup):
        lines.append(_fmt(f"  setup:{k}", _stats(by_setup[k])))
    for k in sorted(by_strat):
        lines.append(_fmt(f"  strat:{k}", _stats(by_strat[k])))

    reasons = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    lines.append("-" * 120)
    lines.append("  exits: " + "  ".join(f"{k}={v} ({v/len(trades)*100:.0f}%)" for k, v in sorted(reasons.items())))
    lines.append("=" * 120)
    return "\n".join(lines)
