"""
Fyers order placement, account funds, and order status.
Used exclusively in live trading mode.
"""
import logging
import time
from typing import Optional

from fyers.auth import get_fyers_client

logger = logging.getLogger(__name__)

# Fyers order status codes
_STATUS_TRADED    = 2
_STATUS_REJECTED  = 5
_STATUS_CANCELLED = 1
_TERMINAL_STATES  = {_STATUS_TRADED, _STATUS_REJECTED, _STATUS_CANCELLED}


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


def get_order_fill(order_id: str, max_attempts: int = 10, interval_s: float = 1.0) -> Optional[dict]:
    """
    Poll Fyers orderbook until the order reaches a terminal state (filled/rejected/cancelled).

    Returns a dict:
        {"status": "TRADED"|"REJECTED"|"CANCELLED"|"TIMEOUT",
         "traded_price": float,   # 0.0 if not filled
         "filled_qty": int}
    Returns None if the API call itself fails.
    """
    fyers = get_fyers_client()
    for attempt in range(1, max_attempts + 1):
        try:
            response = fyers.get_orders({"id": order_id})
            if response.get("s") != "ok":
                logger.warning(f"get_orders failed for {order_id}: {response}")
                return None
            orders = response.get("orderBook") or []
            if not orders:
                logger.warning(f"Order {order_id} not found in orderbook (attempt {attempt})")
            else:
                order = orders[0]
                status_code = order.get("status")
                if status_code in _TERMINAL_STATES:
                    label = {_STATUS_TRADED: "TRADED", _STATUS_REJECTED: "REJECTED",
                             _STATUS_CANCELLED: "CANCELLED"}.get(status_code, "UNKNOWN")
                    traded_price = float(order.get("tradedPrice") or 0.0)
                    filled_qty   = int(order.get("filledQty") or 0)
                    logger.info(
                        f"Order {order_id} → {label} price=₹{traded_price:.2f} qty={filled_qty}"
                    )
                    return {"status": label, "traded_price": traded_price, "filled_qty": filled_qty}
                logger.debug(f"Order {order_id} status={status_code} (attempt {attempt}/{max_attempts})")
        except Exception as e:
            logger.warning(f"Error polling order {order_id} (attempt {attempt}): {e}")

        if attempt < max_attempts:
            time.sleep(interval_s)

    logger.warning(f"Order {order_id} did not reach terminal state in {max_attempts}s")
    return {"status": "TIMEOUT", "traded_price": 0.0, "filled_qty": 0}


def get_fyers_positions() -> Optional[list]:
    """
    Fetch open positions from the Fyers account.
    Returns a list of net position dicts with non-zero netQty, or None if the call fails.
    """
    fyers = get_fyers_client()
    try:
        response = fyers.positions()
        if response.get("s") != "ok":
            logger.warning(f"Fyers positions API error: {response}")
            return None
        return [p for p in (response.get("netPositions") or []) if int(p.get("netQty", 0)) != 0]
    except Exception as e:
        logger.exception(f"Error fetching Fyers positions: {e}")
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
