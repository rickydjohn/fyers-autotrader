"""
The EOD screen.

For each symbol in the universe:
    daily bars → features → liquidity/price gate → signals → aggregate → plan

A symbol that several strategies flag (confluence) ranks above one with a single
flag. Output is a ranked list of Candidates, each carrying its signals and a sized
TradePlan — i.e. tomorrow's watchlist with entry/stop/target.

The screen takes a CandleProvider, so it runs against live Fyers (CoreEngineProvider)
or a synthetic provider in tests/backtests with zero code change.
"""

import logging
from typing import Optional

from config import settings
from data import CandleProvider
from features import build_features
from models import Candidate, EquitySymbol, Features, Signal
from risk import build_plan
from signals import generate_signals

logger = logging.getLogger(__name__)


def _passes_liquidity(f: Features) -> bool:
    return (
        settings.min_price <= f.ltp <= settings.max_price
        and f.avg_turnover_cr >= settings.min_avg_turnover_cr
    )


def _aggregate(sym: EquitySymbol, signals: list[Signal]) -> Candidate:
    """Confluence-aware ranking: best single score + a small bonus per extra signal."""
    best = max(signals, key=lambda s: s.score)
    confluence_bonus = 0.05 * (len(signals) - 1)
    rank_score = min(1.0, best.score + confluence_bonus)
    return Candidate(
        symbol=sym.symbol,
        rank_score=round(rank_score, 3),
        setup_type=best.setup_type,
        side=best.side,
        signals=signals,
    )


def screen(
    symbols: list[EquitySymbol],
    provider: CandleProvider,
    capital: Optional[float] = None,
) -> list[Candidate]:
    candidates: list[Candidate] = []
    scanned = skipped_data = skipped_liq = no_signal = no_plan = 0

    for sym in symbols:
        scanned += 1
        bars = provider.daily_bars(sym.symbol, limit=settings.daily_lookback_bars)
        f = build_features(sym.symbol, bars)
        if f is None:
            skipped_data += 1
            continue
        if not _passes_liquidity(f):
            skipped_liq += 1
            continue

        signals = generate_signals(f)
        if not signals:
            no_signal += 1
            continue

        cand = _aggregate(sym, signals)
        best = max(signals, key=lambda s: s.score)
        plan = build_plan(f, best, capital)
        if plan is None:
            no_plan += 1
            continue
        cand.plan = plan
        candidates.append(cand)

    candidates.sort(key=lambda c: c.rank_score, reverse=True)
    logger.info(
        "Screen: scanned=%d → candidates=%d (skipped: data=%d liquidity=%d no_signal=%d no_plan=%d)",
        scanned, len(candidates), skipped_data, skipped_liq, no_signal, no_plan,
    )
    return candidates
