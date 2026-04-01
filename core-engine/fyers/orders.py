"""
Fyers order placement and account funds.
Used exclusively in live trading mode.
"""
import logging
from typing import Optional

from fyers.auth import get_fyers_client

logger = logging.getLogger(__name__)


def place_market_order(symbol: str, side: str, quantity: int) -> Optional[dict]:
    """Place a DAY market order via Fyers. side='BUY'|'SELL'. Returns response or None."""
    fyers = get_fyers_client()
    payload = {
        "symbol": symbol,
        "qty": quantity,
        "type": 2,                             # 2 = market order
        "side": 1 if side == "BUY" else -1,   # 1=buy, -1=sell
        "productType": "INTRADAY",
        "limitPrice": 0,
        "stopPrice": 0,
        "validity": "DAY",
        "disclosedQty": 0,
        "offlineOrder": False,
    }
    try:
        response = fyers.place_order(data=payload)
        if response.get("s") != "ok":
            logger.error(f"Fyers order failed {symbol} {side} {quantity}: {response}")
            return None
        logger.info(f"Live order placed: {side} {quantity}x{symbol} → id={response.get('id')}")
        return response
    except Exception as e:
        logger.exception(f"Error placing Fyers order {symbol}: {e}")
        return None


def get_funds() -> Optional[dict]:
    """Fetch account fund details from Fyers. Returns dict keyed by fund title or None."""
    fyers = get_fyers_client()
    try:
        response = fyers.funds()
        if response.get("s") != "ok":
            logger.error(f"Fyers funds error: {response}")
            return None
        fund_limit = response.get("fund_limit", [])
        result = {}
        for item in fund_limit:
            key = item.get("title", "").lower().replace(" ", "_")
            result[key] = round(float(item.get("equityAmount", 0)), 2)
        return result
    except Exception as e:
        logger.exception(f"Error fetching Fyers funds: {e}")
        return None
