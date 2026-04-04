"""
Option chain utilities: select ATM option for a given decision.

All option metadata (symbol, expiry, lot size) is fetched from Fyers directly:
  - Symbol & expiry: from fyers.optionchain() — no local symbol-building needed,
    works for both NIFTY weekly and BANKNIFTY monthly formats automatically.
  - Lot size: GCD of bid/ask volumes from fyers.depth().
    NSE enforces all order quantities as multiples of lot size → GCD == lot size.

Both are cached per underlying for the duration of the process.
"""

import logging
from math import gcd
from functools import reduce
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Strike-price intervals per underlying (for nearest-strike lookup)
STRIKE_INTERVALS = {
    "NSE:NIFTY50-INDEX": 50,
    "NSE:NIFTYBANK-INDEX": 100,
}

SUPPORTED_UNDERLYINGS = set(STRIKE_INTERVALS.keys())

# In-process caches — reset on service restart
_lot_size_cache: dict = {}  # {underlying: int}


def _fetch_lot_size_from_depth(option_symbol: str) -> Optional[int]:
    """
    Derive lot size from Fyers market depth.
    All NSE F&O order quantities are multiples of the lot size,
    so GCD of bid/ask volumes equals the lot size.
    """
    from fyers.auth import get_fyers_client
    try:
        fyers = get_fyers_client()
        resp = fyers.depth(data={"symbol": option_symbol, "ohlcv_flag": 1})
        depth_data = resp.get("d", {}).get(option_symbol, {})
        volumes = [
            entry["volume"]
            for side in ("bids", "ask")
            for entry in depth_data.get(side, [])
            if entry.get("volume", 0) > 0
        ]
        if not volumes:
            logger.warning(f"Empty depth for {option_symbol}, cannot derive lot size")
            return None
        lot_size = reduce(gcd, volumes)
        logger.info(f"Lot size for {option_symbol} derived from Fyers depth: {lot_size}")
        return lot_size
    except Exception as e:
        logger.warning(f"Depth call failed for {option_symbol}: {e}")
        return None


def get_atm_option(
    underlying: str,
    ltp: float,
    decision: str,
) -> Optional[Tuple[str, int, str, str, int]]:
    """
    Select the ATM option for a BUY (CE) or SELL (PE) decision.

    Fetches the option chain from Fyers, picks the nearest available strike to
    `ltp`, and returns the symbol Fyers itself knows about — no local symbol
    formatting needed.

    Lot size is derived from Fyers market depth and cached for the session.

    Returns (option_symbol, strike, option_type, expiry_iso, lot_size) or None.
    """
    if underlying not in SUPPORTED_UNDERLYINGS:
        logger.warning(f"No option config for {underlying}")
        return None

    from fyers.auth import get_fyers_client
    try:
        fyers = get_fyers_client()
        resp = fyers.optionchain(data={
            "symbol": underlying,
            "strikecount": 10,
            "timestamp": "",
        })
        chain = resp.get("data", {}).get("optionsChain", [])
        expiry_data = resp.get("data", {}).get("expiryData", [])
    except Exception as e:
        logger.error(f"Option chain fetch failed for {underlying}: {e}")
        return None

    if not chain or not expiry_data:
        logger.warning(f"Empty option chain for {underlying}")
        return None

    target_type = "CE" if decision == "BUY" else "PE"

    # Filter to actual option entries with the target type
    options = [
        e for e in chain
        if e.get("strike_price", -1) > 0 and e.get("option_type") == target_type
    ]
    if not options:
        logger.warning(f"No {target_type} options in chain for {underlying}")
        return None

    # Pick the option whose strike is closest to current LTP
    best = min(options, key=lambda e: abs(e["strike_price"] - ltp))
    option_symbol = best["symbol"]
    strike = int(best["strike_price"])

    # Parse expiry from the first entry in expiryData (DD-MM-YYYY)
    try:
        date_str = expiry_data[0]["date"]   # e.g. "07-04-2026"
        dd, mm, yyyy = date_str.split("-")
        from datetime import date
        expiry_iso = date(int(yyyy), int(mm), int(dd)).isoformat()
    except Exception:
        expiry_iso = ""

    # Lot size — cached per underlying
    if underlying not in _lot_size_cache:
        lot_size = _fetch_lot_size_from_depth(option_symbol)
        if lot_size:
            _lot_size_cache[underlying] = lot_size
        else:
            logger.warning(f"Lot size unavailable for {underlying}, defaulting to 1")
            lot_size = 1
    else:
        lot_size = _lot_size_cache[underlying]

    logger.info(
        f"ATM option for {underlying} ({decision}): {option_symbol} "
        f"strike={strike} expiry={expiry_iso} lot_size={lot_size}"
    )
    return option_symbol, strike, target_type, expiry_iso, lot_size
