"""
Technical indicator calculations using pure numpy/pandas.
All functions accept a list of OHLCBar and return scalar values.
"""

from typing import List, Tuple

import numpy as np
import pandas as pd

from models.schemas import OHLCBar


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
