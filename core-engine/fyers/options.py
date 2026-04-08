"""
Option chain utilities: select an affordable option for a given decision.

All option metadata (symbol, expiry, lot size) is fetched from Fyers directly:
  - Symbol & expiry: from fyers.optionchain() — no local symbol-building needed,
    works for both NIFTY weekly and BANKNIFTY monthly formats automatically.
  - Lot size: GCD of bid/ask volumes from fyers.depth().
    NSE enforces all order quantities as multiples of lot size → GCD == lot size.

Both are cached per underlying for the duration of the process.

Budget-aware selection: starts at ATM and walks OTM up to MAX_OTM_STEPS strikes
until a strike whose total cost (premium × lot_size) fits within max_spend.
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

# Maximum OTM strikes to try beyond ATM before giving up
MAX_OTM_STEPS = 3

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


def _get_premium(opt: dict, option_symbol: str) -> float:
    """Extract option premium from chain entry, falling back to a live quote."""
    premium = float(opt.get("ltp") or opt.get("ask") or 0.0)
    if premium > 0:
        return premium
    try:
        from fyers.market_data import get_quote
        q = get_quote(option_symbol)
        return float(q["ltp"]) if q and q.get("ltp") else 0.0
    except Exception:
        return 0.0


def get_affordable_option(
    underlying: str,
    ltp: float,
    decision: str,
    max_spend: Optional[float] = None,
) -> Optional[Tuple[str, int, str, str, int]]:
    """
    Select the best affordable option for a BUY (CE) or SELL (PE) decision.

    Starts at ATM (strike closest to ltp). If max_spend is provided and the
    ATM premium × lot_size exceeds it, walks OTM one strike at a time (up to
    MAX_OTM_STEPS) until an affordable strike is found.

    OTM direction:
      CE (BUY)  → higher strikes are OTM
      PE (SELL) → lower strikes are OTM

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

    options = [
        e for e in chain
        if e.get("strike_price", -1) > 0 and e.get("option_type") == target_type
    ]
    if not options:
        logger.warning(f"No {target_type} options in chain for {underlying}")
        return None

    # Parse expiry once
    try:
        date_str = expiry_data[0]["date"]   # e.g. "07-04-2026"
        dd, mm, yyyy = date_str.split("-")
        from datetime import date
        expiry_iso = date(int(yyyy), int(mm), int(dd)).isoformat()
    except Exception:
        expiry_iso = ""

    # Find ATM strike (closest to LTP)
    atm = min(options, key=lambda e: abs(e["strike_price"] - ltp))
    atm_strike = atm["strike_price"]

    # Build candidate list: ATM first, then increasingly OTM
    if target_type == "CE":
        # OTM for calls = higher strikes
        candidates = sorted(
            [e for e in options if e["strike_price"] >= atm_strike],
            key=lambda e: e["strike_price"],
        )
    else:
        # OTM for puts = lower strikes
        candidates = sorted(
            [e for e in options if e["strike_price"] <= atm_strike],
            key=lambda e: e["strike_price"],
            reverse=True,
        )

    candidates = candidates[: MAX_OTM_STEPS + 1]

    # Resolve lot size once (cached per underlying)
    if underlying not in _lot_size_cache:
        lot_size = _fetch_lot_size_from_depth(candidates[0]["symbol"])
        if lot_size:
            _lot_size_cache[underlying] = lot_size
        else:
            logger.warning(f"Lot size unavailable for {underlying}, defaulting to 1")
            lot_size = 1
    else:
        lot_size = _lot_size_cache[underlying]

    # Walk from ATM outward; pick first strike that fits the budget
    for i, opt in enumerate(candidates):
        option_symbol = opt["symbol"]
        strike = int(opt["strike_price"])
        premium = _get_premium(opt, option_symbol)

        if premium <= 0:
            logger.debug(f"No premium data for strike {strike}, skipping")
            continue

        total_cost = premium * lot_size
        otm_label = "ATM" if i == 0 else f"OTM+{i}"

        if max_spend is None or total_cost <= max_spend:
            if i > 0:
                logger.info(
                    f"[BUDGET] {underlying} ({decision}) ATM unaffordable — "
                    f"selected {otm_label} strike {strike} @ ₹{premium:.2f} "
                    f"(cost ₹{total_cost:.0f} ≤ budget ₹{max_spend:.0f})"
                )
            else:
                logger.info(
                    f"ATM option for {underlying} ({decision}): {option_symbol} "
                    f"strike={strike} expiry={expiry_iso} lot_size={lot_size}"
                )
            return option_symbol, strike, target_type, expiry_iso, lot_size

    # No affordable strike found within the OTM walk limit
    atm_premium = _get_premium(candidates[0], candidates[0]["symbol"])
    logger.warning(
        f"[BUDGET] No affordable {target_type} option for {underlying} ({decision}): "
        f"ATM strike {int(atm_strike)} costs ₹{atm_premium * lot_size:.0f} "
        f"(₹{atm_premium:.2f} × {lot_size} lots), budget ₹{max_spend:.0f}. "
        f"Tried {len(candidates)} strike(s) — skipping trade."
    )
    return None


# Keep old name as an alias for any callers that haven't been updated
def get_atm_option(
    underlying: str,
    ltp: float,
    decision: str,
) -> Optional[Tuple[str, int, str, str, int]]:
    return get_affordable_option(underlying, ltp, decision, max_spend=None)
