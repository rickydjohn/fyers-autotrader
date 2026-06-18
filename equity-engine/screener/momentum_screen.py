"""
Momentum screener — the discretionary tool.

Ranks the liquid, ETF-free universe by 12-1 momentum (the one factor with a real, if
modest, premium) and attaches the context a human needs to judge each name: trend
regime, distance from the 52-week high, monthly-CPR position, RSI, and an ATR-based
reference stop/target. It is explicitly NOT a mechanical strategy — the 14-year test
showed factor backtests on this data are survivorship-corrupted. This surfaces and
explains candidates; the trading decision stays with the user.
"""

import logging

from data import CandleProvider
from features import build_features
from models import EquitySymbol
from universe import is_etf

logger = logging.getLogger(__name__)

LOOKBACK = 252   # 12 months
SKIP = 21        # skip the most recent month (12-1 momentum)
ATR_STOP_MULT = 2.0
ATR_TARGET_MULT = 4.0


def momentum_watchlist(
    symbols: list[EquitySymbol],
    provider: CandleProvider,
    top_n: int = 30,
    min_turnover_cr: float = 10.0,
    history: int = 320,
) -> list[dict]:
    scored = []
    for sym in symbols:
        if is_etf(sym.short_symbol):
            continue
        bars = provider.daily_bars(sym.symbol, limit=history)
        if len(bars) < LOOKBACK + SKIP + 5:
            continue
        recent = bars[-20:]
        turnover_cr = sum(b.close * b.volume for b in recent) / len(recent) / 1e7
        if turnover_cr < min_turnover_cr:
            continue
        base = bars[-1 - SKIP - LOOKBACK].close
        if base <= 0:
            continue
        f = build_features(sym.symbol, bars)
        if f is None:
            continue
        mom = bars[-1 - SKIP].close / base - 1.0
        scored.append((mom, sym, f, turnover_cr))

    scored.sort(key=lambda x: x[0], reverse=True)
    total = len(scored)

    rows = []
    for rank, (mom, sym, f, turnover_cr) in enumerate(scored[:top_n], 1):
        pctile = round((1 - (rank - 1) / max(1, total - 1)) * 100)
        entry = f.ltp
        rows.append({
            "rank": rank,
            "symbol": sym.symbol,
            "name": sym.short_symbol,
            "ltp": entry,
            "momentum_12_1_pct": round(mom * 100, 1),
            "momentum_pctile": pctile,
            "regime": f.regime.value,
            "pct_from_52w_high": f.pct_from_52w_high,
            "monthly_cpr": f.monthly_cpr.position,
            "rsi": f.rsi,
            "atr_pct": f.atr_pct,
            "turnover_cr": round(turnover_cr, 1),
            "ref_entry": entry,
            "ref_stop": round(entry - ATR_STOP_MULT * f.atr, 2),
            "ref_target": round(entry + ATR_TARGET_MULT * f.atr, 2),
            "rationale": (
                f"#{rank} of {total} liquid names by 12-1 momentum "
                f"({mom*100:+.0f}% trailing 12m, ~{pctile}th pctile); "
                f"{f.regime.value.lower()}; {f.pct_from_52w_high:+.0f}% from 52w high; "
                f"monthly CPR {f.monthly_cpr.position.replace('_', ' ').lower()}; RSI {f.rsi:.0f}"
            ),
        })
    logger.info("Momentum screen: %d liquid names ranked, top %d returned", total, len(rows))
    return rows
