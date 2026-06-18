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

# Approx candles per TRADING day, by resolution — used to size the calendar-day
# lookback so `limit` bars actually come back. The old `limit//75` heuristic was
# calibrated only for 5m, so daily/hourly fetches came back nearly empty.
_CANDLES_PER_DAY = {"1": 375, "5": 75, "15": 25, "60": 7, "D": 1}
# Fyers caps the date range per single history request, by resolution (see
# get_historical_candles_daterange). Larger windows must be chunked + concatenated.
_MAX_DAYS_PER_REQ = {"1": 30, "5": 90, "15": 90, "60": 100, "D": 365}

_DATE_FMT = "%Y-%m-%d"


def get_historical_candles(
    symbol: str,
    interval: str = "1m",
    limit: int = 200,
) -> List[OHLCBar]:
    """Fetch the most recent `limit` OHLCV candles for a symbol.

    Sizes the lookback window per resolution and, when that window exceeds Fyers'
    per-request cap (e.g. daily history > 365d), splits it into chunks and
    concatenates. Intraday windows stay small → single request, unchanged behaviour.
    """
    resolution = RESOLUTION_MAP.get(interval, "5")
    cpd = _CANDLES_PER_DAY.get(resolution, 75)
    max_days = _MAX_DAYS_PER_REQ.get(resolution, 90)

    # trading days needed → calendar days (×1.5 for weekends/holidays + buffer)
    lookback_days = max(5, int(limit / cpd * 1.5) + 5)
    now_ist = datetime.now(IST)

    if lookback_days <= max_days:
        date_from = (now_ist - timedelta(days=lookback_days)).strftime(_DATE_FMT)
        bars = get_historical_candles_daterange(
            symbol, interval, date_from, now_ist.strftime(_DATE_FMT)
        )
    else:
        # Walk backwards in <= max_days windows, oldest-first concatenation.
        bars = []
        end = now_ist
        remaining = lookback_days
        while remaining > 0:
            span = min(max_days, remaining)
            start = end - timedelta(days=span)
            chunk = get_historical_candles_daterange(
                symbol, interval, start.strftime(_DATE_FMT), end.strftime(_DATE_FMT)
            )
            bars = chunk + bars
            end = start - timedelta(days=1)
            remaining -= span
        # Dedup boundary overlaps, keep chronological order.
        seen, deduped = set(), []
        for b in sorted(bars, key=lambda x: x.timestamp):
            if b.timestamp not in seen:
                seen.add(b.timestamp)
                deduped.append(b)
        bars = deduped

    return bars[-limit:]


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


# NSE sector sub-indices with approximate NIFTY 50 weight contributions.
# Symbols use Fyers' CNX prefix (NSE's legacy CRISIL naming).
# Weights are approximate and reviewed quarterly by NSE — update if index
# composition changes significantly. Total coverage: ~74% of NIFTY weight.
SECTOR_INDICES: dict = {
    "BANK":   ("NSE:NIFTYBANK-INDEX",   35),
    "IT":     ("NSE:NIFTYIT-INDEX",     14),
    "FMCG":   ("NSE:NIFTYFMCG-INDEX",   9),
    "AUTO":   ("NSE:NIFTYAUTO-INDEX",    7),
    "PHARMA": ("NSE:NIFTYPHARMA-INDEX",  5),
    "METAL":  ("NSE:NIFTYMETAL-INDEX",   4),
}


def get_sector_breadth() -> dict:
    """Fetch real-time quotes for NSE sector sub-indices in one batch call.

    Returns a dict keyed by sector name:
        {
          "BANK": {"change_pct": -0.82, "ltp": 51234.5, "weight": 35},
          "IT":   {"change_pct": -0.31, "ltp": 37890.2, "weight": 14},
          ...
        }
    Sectors that fail to quote are silently omitted so callers degrade
    gracefully when a symbol string is wrong or Fyers is unavailable.
    """
    fyers = get_fyers_client()
    symbols_str = ",".join(sym for sym, _wt in SECTOR_INDICES.values())
    try:
        response = fyers.quotes(data={"symbols": symbols_str})
        if response.get("s") != "ok":
            logger.warning(f"Sector breadth batch quote failed: {response}")
            return {}

        # Build lookup by Fyers symbol name from response
        sym_to_data: dict = {}
        for entry in response.get("d", []):
            n = entry.get("n", "")
            v = entry.get("v", {})
            sym_to_data[n] = v

        result: dict = {}
        for sector, (fyers_sym, weight) in SECTOR_INDICES.items():
            v = sym_to_data.get(fyers_sym)
            if not v:
                logger.debug(f"No quote data for sector {sector} ({fyers_sym})")
                continue
            result[sector] = {
                "ltp":        float(v.get("lp", 0) or 0),
                "change_pct": float(v.get("chp", 0) or 0),
                "weight":     weight,
                "symbol":     fyers_sym,
            }
        return result
    except Exception as e:
        logger.exception(f"Error fetching sector breadth: {e}")
        return {}


def get_previous_day_ohlc(symbol: str) -> Optional[dict]:
    """Get previous trading day OHLC for CPR calculation."""
    candles = get_historical_candles(symbol, interval="1d", limit=2)
    if len(candles) < 1:
        return None
    # Use the most recent completed day
    c = candles[-1] if len(candles) == 1 else candles[-2]
    return {"open": c.open, "high": c.high, "low": c.low, "close": c.close}
