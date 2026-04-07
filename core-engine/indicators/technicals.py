"""
Technical indicator calculations using pure numpy/pandas.
All functions accept a list of OHLCBar and return scalar values.
"""

from typing import List, Tuple

import numpy as np
import pandas as pd
import pytz

from models.schemas import OHLCBar

_IST = pytz.timezone("Asia/Kolkata")


def _to_series(candles: List[OHLCBar]) -> Tuple[pd.Series, pd.Series, pd.Series, pd.Series, pd.Series]:
    closes = pd.Series([c.close for c in candles], dtype=float)
    opens = pd.Series([c.open for c in candles], dtype=float)
    highs = pd.Series([c.high for c in candles], dtype=float)
    lows = pd.Series([c.low for c in candles], dtype=float)
    volumes = pd.Series([c.volume for c in candles], dtype=float)
    return closes, opens, highs, lows, volumes


def calculate_rsi(candles: List[OHLCBar], period: int = 14) -> float:
    closes, *_ = _to_series(candles)
    if len(closes) < period + 1:
        return 50.0
    delta = closes.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return round(float(rsi.iloc[-1]), 2)


def calculate_macd(
    candles: List[OHLCBar],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> Tuple[float, float, float]:
    closes, *_ = _to_series(candles)
    if len(closes) < slow + signal:
        return 0.0, 0.0, 0.0
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return (
        round(float(macd_line.iloc[-1]), 4),
        round(float(signal_line.iloc[-1]), 4),
        round(float(histogram.iloc[-1]), 4),
    )


def calculate_ema(candles: List[OHLCBar], period: int) -> float:
    closes, *_ = _to_series(candles)
    if len(closes) < period:
        return float(closes.iloc[-1])
    return round(float(closes.ewm(span=period, adjust=False).mean().iloc[-1]), 2)


def calculate_vwap(candles: List[OHLCBar]) -> float:
    closes, opens, highs, lows, volumes = _to_series(candles)
    typical_price = (highs + lows + closes) / 3
    cumulative_tp_vol = (typical_price * volumes).cumsum()
    cumulative_vol = volumes.cumsum()
    vwap = cumulative_tp_vol / cumulative_vol.replace(0, np.nan)
    return round(float(vwap.iloc[-1]), 2)


def get_macd_signal_label(macd: float, signal: float) -> str:
    if macd > signal:
        return "BULLISH"
    elif macd < signal:
        return "BEARISH"
    return "NEUTRAL"


def calculate_day_range(candles: List[OHLCBar]) -> Tuple[float, float]:
    """
    Return (day_high, day_low) from today's IST session candles only.
    Falls back to the full candle set if no today-candles are found.
    """
    import datetime as _dt
    today = _dt.datetime.now(_IST).date()
    today_candles = [
        c for c in candles
        if (
            c.timestamp.astimezone(_IST).date()
            if c.timestamp.tzinfo
            else c.timestamp.replace(tzinfo=_IST).date()
        ) == today
    ]
    source = today_candles if today_candles else candles
    _, _, highs, lows, _ = _to_series(source)
    return round(float(highs.max()), 2), round(float(lows.min()), 2)


def calculate_consolidation(
    candles: List[OHLCBar],
    lookback: int = 8,
    threshold_pct: float = 0.40,
) -> Tuple[float, float, float]:
    """
    Measure how sideways the market has been over the last `lookback` completed
    candles (the current/incomplete candle is excluded).

    Returns:
        consolidation_pct  — (high - low) / midpoint * 100 over the window
        window_high        — top of the consolidation band
        window_low         — bottom of the consolidation band

    A consolidation_pct below `threshold_pct` (default 0.40%) means the market
    has been moving sideways; the caller uses window_high / window_low to decide
    whether the current LTP has broken out of that band.
    """
    # Exclude the latest (possibly incomplete) candle
    window = candles[-(lookback + 1):-1]
    if len(window) < 3:
        last = candles[-1]
        return 0.0, last.high, last.low

    _, _, highs, lows, _ = _to_series(window)
    w_high = float(highs.max())
    w_low = float(lows.min())
    midpoint = (w_high + w_low) / 2
    pct = (w_high - w_low) / midpoint * 100 if midpoint > 0 else 0.0
    return round(pct, 3), round(w_high, 2), round(w_low, 2)
