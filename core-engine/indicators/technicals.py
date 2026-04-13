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
    closes, opens, highs, lows, volumes = _to_series(source)
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


def _summarise_candles(candles: List[OHLCBar]) -> str:
    """One-line pattern summary over the candle window."""
    if not candles:
        return "No data."

    last6 = candles[-6:]
    bullish_count = sum(1 for c in last6 if c.close > c.open)
    bearish_count = sum(1 for c in last6 if c.close < c.open)

    last4 = candles[-4:]
    higher_lows = (
        len(last4) >= 2 and all(last4[i].low > last4[i - 1].low for i in range(1, len(last4)))
    )
    lower_highs = (
        len(last4) >= 2 and all(last4[i].high < last4[i - 1].high for i in range(1, len(last4)))
    )

    closes = [c.close for c in last6]
    close_range = max(closes) - min(closes)

    parts = [f"Last 6: {bullish_count} bullish, {bearish_count} bearish"]
    if higher_lows:
        parts.append("higher lows (buyers stepping up)")
    elif lower_highs:
        parts.append("lower highs (sellers in control)")

    if close_range < 30:
        parts.append(f"consolidating {min(closes):.0f}–{max(closes):.0f} ({close_range:.0f} pt range)")
    else:
        parts.append(f"active range {close_range:.0f} pts")

    return " | ".join(parts)


def format_candles_for_prompt(candles: List[OHLCBar], lookback: int = 12) -> str:
    """
    Format the last `lookback` today-only 5m candles as a structured block for the LLM.
    Each row shows OHLC, % change from open, body classification, and wick notes.
    Body strength is derived from body/range ratio (self-normalising across volatility regimes).
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

    if not today_candles:
        return "  No intraday candles available for today."

    window = today_candles[-lookback:]

    header = f"{'Time':5}  {'Open':>8}  {'High':>8}  {'Low':>8}  {'Close':>8}  {'Chg%':>6}  {'Body':<22}  Wick"
    sep = "─" * 90
    rows = [header, sep]

    for c in window:
        ist_time = c.timestamp.astimezone(_IST).strftime("%H:%M")
        body = abs(c.close - c.open)
        candle_range = c.high - c.low
        is_bullish = c.close >= c.open
        chg_pct = (c.close - c.open) / c.open * 100 if c.open else 0.0

        # Body classification by body/range ratio
        if candle_range == 0 or body / candle_range < 0.15:
            body_label = "Doji"
        elif body / candle_range < 0.40:
            body_label = "Bullish (weak)" if is_bullish else "Bearish (weak)"
        elif body / candle_range < 0.65:
            body_label = "Bullish" if is_bullish else "Bearish"
        else:
            body_label = "Bullish (strong)" if is_bullish else "Bearish (strong)"

        # Wick classification
        upper_wick = c.high - max(c.open, c.close)
        lower_wick = min(c.open, c.close) - c.low
        wick_parts = []

        if candle_range > 0:
            for wick_size, label in ((lower_wick, "lower"), (upper_wick, "upper")):
                if wick_size > 2 * body and wick_size / candle_range > 0.40:
                    wick_parts.append(f"{label} {wick_size:.0f} pt rejection")
                elif wick_size > body and wick_size / candle_range > 0.30:
                    wick_parts.append(f"{label} {wick_size:.0f} pt notable")

        wick_note = "; ".join(wick_parts)

        rows.append(
            f"{ist_time:5}  {c.open:>8.1f}  {c.high:>8.1f}  {c.low:>8.1f}  {c.close:>8.1f}  "
            f"{chg_pct:>+5.2f}%  {body_label:<22}  {wick_note}"
        )

    rows.append(sep)
    rows.append(f"Pattern: {_summarise_candles(window)}")
    return "\n".join(rows)


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
