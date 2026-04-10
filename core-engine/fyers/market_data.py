"""
Fetches market data from Fyers API.
Handles both historical OHLCV and live quote data.
"""

import logging
from datetime import datetime, timedelta
from typing import List, Optional

import pytz

from config import settings
from fyers.auth import get_fyers_client
from models.schemas import OHLCBar

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

RESOLUTION_MAP = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "1h": "60",
    "1d": "D",
}


def get_historical_candles(
    symbol: str,
    interval: str = "5m",
    limit: int = 200,
) -> List[OHLCBar]:
    """Fetch historical OHLCV candles for a symbol."""
    fyers = get_fyers_client()
    resolution = RESOLUTION_MAP.get(interval, "5")
    now_ist = datetime.now(IST)
    date_format = "%Y-%m-%d"

    # Look back enough days to fill limit candles (accounting for weekends/holidays)
    lookback_days = max(5, limit // 75 + 2)
    date_from = (now_ist - timedelta(days=lookback_days)).strftime(date_format)
    date_to = now_ist.strftime(date_format)

    payload = {
        "symbol": symbol,
        "resolution": resolution,
        "date_format": "1",  # epoch timestamp
        "range_from": date_from,
        "range_to": date_to,
        "cont_flag": "1",
    }

    try:
        response = fyers.history(data=payload)
        if response.get("s") != "ok":
            logger.error(f"Fyers history error for {symbol}: {response}")
            return []

        candles = []
        for row in response.get("candles", [])[-limit:]:
            ts, o, h, l, c, v = row
            candles.append(
                OHLCBar(
                    timestamp=datetime.fromtimestamp(ts, tz=IST),
                    open=o,
                    high=h,
                    low=l,
                    close=c,
                    volume=int(v),
                )
            )
        return candles
    except Exception as e:
        logger.exception(f"Error fetching candles for {symbol}: {e}")
        return []


def get_quote(symbol: str) -> Optional[dict]:
    """Fetch real-time quote for a symbol."""
    fyers = get_fyers_client()
    try:
        response = fyers.quotes(data={"symbols": symbol})
        if response.get("s") != "ok":
            logger.error(f"Fyers quote error: {response}")
            return None
        d = response.get("d", [{}])[0].get("v", {})
        return {
            "symbol": symbol,
            "ltp": d.get("lp", 0),
            "open": d.get("open_price", 0),
            "high": d.get("high_price", 0),
            "low": d.get("low_price", 0),
            "close": d.get("prev_close_price", 0),
            "volume": d.get("volume", 0),
            "change": d.get("ch", 0),
            "change_pct": d.get("chp", 0),
        }
    except Exception as e:
        logger.exception(f"Error fetching quote for {symbol}: {e}")
        return None


def get_historical_candles_daterange(
    symbol: str,
    interval: str,
    date_from: str,
    date_to: str,
) -> List[OHLCBar]:
    """
    Fetch OHLCV candles for an explicit date range (YYYY-MM-DD strings).
    Used for bootstrapping historical context on startup.
    Fyers limits: 1m=30d, 5m/15m=90d, 1h=100d, daily=365d per request.
    """
    fyers = get_fyers_client()
    resolution = RESOLUTION_MAP.get(interval, "5")
    payload = {
        "symbol": symbol,
        "resolution": resolution,
        "date_format": "1",
        "range_from": date_from,
        "range_to": date_to,
        "cont_flag": "1",
    }
    try:
        response = fyers.history(data=payload)
        if response.get("s") != "ok":
            logger.error(f"Fyers history error {symbol} {interval} {date_from}→{date_to}: {response}")
            return []
        candles = []
        for row in response.get("candles", []):
            ts, o, h, l, c, v = row
            candles.append(OHLCBar(
                timestamp=datetime.fromtimestamp(ts, tz=IST),
                open=o, high=h, low=l, close=c, volume=int(v),
            ))
        return candles
    except Exception as e:
        logger.exception(f"Error fetching candles range {symbol} {interval}: {e}")
        return []


def get_previous_day_ohlc(symbol: str) -> Optional[dict]:
    """Get previous trading day OHLC for CPR calculation."""
    candles = get_historical_candles(symbol, interval="1d", limit=2)
    if len(candles) < 1:
        return None
    # Use the most recent completed day
    c = candles[-1] if len(candles) == 1 else candles[-2]
    return {"open": c.open, "high": c.high, "low": c.low, "close": c.close}
