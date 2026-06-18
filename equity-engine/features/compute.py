"""
Assemble a Features object for one symbol from its daily bars.

Pure function: (symbol, daily_bars) → Features. No I/O, no Fyers — so it runs
identically in the live scan and in the backtest replay. Returns None when there
isn't enough history to compute a trustworthy view (the screener skips those).
"""

import logging
from typing import Optional

from features import indicators, levels
from models import Bar, Features, TrendRegime

logger = logging.getLogger(__name__)

MIN_BARS = 60        # need a couple of months of daily history to mean anything
TREND_SEP_PCT = 1.0  # the 20-EMA must be ≥ this % from the long anchor to call a trend
                     # (otherwise near-flat EMAs read as a fake "stack" → RANGE)


def _regime(price: float, ema20, ema50, ema200) -> TrendRegime:
    """Stacked-EMA trend classification with a separation floor, so a flat/choppy
    series (EMAs bunched together) is correctly RANGE rather than a fake trend.
    Degrades gracefully when EMA200 is absent (short history)."""
    if not ema50 or ema50 <= 0:
        return TrendRegime.RANGE
    long_anchor = ema200 if ema200 else ema50
    short = ema20 if ema20 else ema50
    spread_pct = (short - long_anchor) / long_anchor * 100.0

    if price > ema50 and short >= ema50 >= long_anchor and spread_pct >= TREND_SEP_PCT:
        return TrendRegime.UPTREND
    if price < ema50 and short <= ema50 <= long_anchor and spread_pct <= -TREND_SEP_PCT:
        return TrendRegime.DOWNTREND
    return TrendRegime.RANGE


def build_features(symbol: str, daily_bars: list[Bar], ltp: Optional[float] = None) -> Optional[Features]:
    if len(daily_bars) < MIN_BARS:
        return None

    closes = [b.close for b in daily_bars]
    price = ltp if ltp else closes[-1]
    if price <= 0:
        return None

    ema20 = indicators.ema(closes, 20)
    ema50 = indicators.ema(closes, 50)
    ema200 = indicators.ema(closes, 200)
    atr_v = indicators.atr(daily_bars) or 0.0
    macd_v, sig_v, hist_v = indicators.macd(closes)
    rsi_v = indicators.rsi(closes)

    monthly = levels.monthly_cpr(daily_bars, price)
    if monthly is None:
        return None
    support, resistance = levels.nearest_sr(daily_bars, price)

    # 20-day average traded value (₹ crore) — liquidity gate input
    recent = daily_bars[-20:]
    avg_turnover_cr = sum(b.close * b.volume for b in recent) / len(recent) / 1e7
    avg_volume = sum(b.volume for b in recent) / len(recent)

    window = daily_bars[-250:]
    hi_52w = max(b.high for b in window)
    lo_52w = min(b.low for b in window)

    return Features(
        symbol=symbol,
        ltp=round(price, 2),
        asof=daily_bars[-1].timestamp,
        regime=_regime(price, ema20, ema50, ema200),
        rsi=round(rsi_v, 2) if rsi_v is not None else 50.0,
        macd=round(macd_v, 4) if macd_v is not None else 0.0,
        macd_signal=round(sig_v, 4) if sig_v is not None else 0.0,
        macd_histogram=round(hist_v, 4) if hist_v is not None else 0.0,
        ema_20=round(ema20, 2) if ema20 else 0.0,
        ema_50=round(ema50, 2) if ema50 else 0.0,
        ema_200=round(ema200, 2) if ema200 else 0.0,
        atr=round(atr_v, 2),
        atr_pct=round(atr_v / price * 100.0, 2) if price else 0.0,
        avg_turnover_cr=round(avg_turnover_cr, 2),
        avg_volume=round(avg_volume, 0),
        monthly_cpr=monthly,
        nearest_support=support,
        nearest_resistance=resistance,
        dist_to_support_pct=round((price - support) / price * 100.0, 2) if support else 0.0,
        dist_to_resistance_pct=round((resistance - price) / price * 100.0, 2) if resistance else 0.0,
        pct_from_52w_high=round((price - hi_52w) / hi_52w * 100.0, 2) if hi_52w else 0.0,
        pct_from_52w_low=round((price - lo_52w) / lo_52w * 100.0, 2) if lo_52w else 0.0,
    )
