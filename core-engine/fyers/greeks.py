"""
Fetches live option quote with Greeks from Fyers API.

Fyers v3 quotes endpoint returns Greeks fields (delta, theta, vega, gamma, iv)
for option symbols in the same call as the LTP — no separate option-chain request needed.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def get_option_quote_with_greeks(option_symbol: str) -> Optional[dict]:
    """
    Fetch live option LTP and Greeks from Fyers.

    Returns a dict with keys:
        symbol, ltp, delta, theta, vega, gamma, iv

    Returns None on API error or if LTP is zero/missing.
    Greeks default to 0.0 when the field is absent from the response
    (e.g. index options on non-expiry days) — callers must treat 0.0 as
    "data unavailable" and skip Greek-based exit rules accordingly.
    """
    from fyers.auth import get_fyers_client
    fyers = get_fyers_client()
    try:
        response = fyers.quotes(data={"symbols": option_symbol})
        if response.get("s") != "ok":
            logger.debug(f"Fyers quote error for {option_symbol}: {response}")
            return None

        v = response.get("d", [{}])[0].get("v", {})
        ltp = v.get("lp", 0)
        if not ltp:
            return None

        return {
            "symbol": option_symbol,
            "ltp":    float(ltp),
            "delta":  float(v.get("delta", 0) or 0),
            "theta":  float(v.get("theta", 0) or 0),
            "vega":   float(v.get("vega",  0) or 0),
            "gamma":  float(v.get("gamma", 0) or 0),
            "iv":     float(v.get("iv",    0) or 0),
        }
    except Exception as e:
        logger.debug(f"Greeks fetch failed for {option_symbol}: {e}")
        return None
