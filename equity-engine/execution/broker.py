"""
Mode-routed equity execution + merged position view.

  execute(symbol, side, qty, confirm):
    mode == 'simulation' → PAPER fill at current LTP, recorded in Redis.
    mode == 'live'       → REAL Fyers CNC (delivery) order via core-engine, but ONLY
                           when confirm=True; otherwise returns confirm_required so the
                           UI can show a confirmation step. Default mode is simulation.

  list_positions():
    real Fyers holdings (tagged ACTUAL) + paper positions (tagged PAPER), each with
    buy price, qty, LTP and P&L — the "stocks we hold" section.

Sync; API handlers call these via asyncio.to_thread.
"""

import logging
from datetime import datetime

import httpx
import pytz

from config import settings
from data import get_provider
from execution import store

logger = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")


def _ltp(symbol: str) -> float:
    q = get_provider().quote(symbol)
    return float(q.get("ltp") or 0.0) if q else 0.0


# ── Execution ─────────────────────────────────────────────────────────────────
def execute(symbol: str, side: str, qty: int, confirm: bool = False) -> dict:
    side = side.upper()
    if side not in ("BUY", "SELL") or qty <= 0:
        return {"status": "error", "detail": "side must be BUY/SELL and qty > 0"}

    mode = store.get_mode()
    if mode == "live":
        if not confirm:
            return {"status": "confirm_required", "mode": "live",
                    "message": f"LIVE {side} {qty} {symbol} will place a REAL order. Re-submit with confirm=true.",
                    "symbol": symbol, "side": side, "qty": qty}
        return _live(symbol, side, qty)
    return _paper(symbol, side, qty)


def _paper(symbol: str, side: str, qty: int) -> dict:
    price = _ltp(symbol)
    if price <= 0:
        return {"status": "error", "detail": f"no quote for {symbol}"}
    now = datetime.now(IST).isoformat()

    if side == "BUY":
        ex = store.get_paper_position(symbol)
        if ex:
            total = ex["qty"] + qty
            ex["avg_price"] = round((ex["avg_price"] * ex["qty"] + price * qty) / total, 2)
            ex["qty"] = total
        else:
            ex = {"symbol": symbol, "name": symbol.split(":")[-1].replace("-EQ", ""),
                  "qty": qty, "avg_price": round(price, 2), "opened_at": now, "source": "PAPER"}
        store.upsert_paper_position(ex)
        store.log_paper_trade({"symbol": symbol, "side": "BUY", "qty": qty, "price": round(price, 2), "ts": now})
        return {"status": "ok", "mode": "paper", "action": "BUY", "fill": round(price, 2), "position": ex}

    # SELL
    ex = store.get_paper_position(symbol)
    if not ex:
        return {"status": "error", "detail": f"no paper position in {symbol} to sell"}
    sell_qty = min(qty, ex["qty"])
    realized = round((price - ex["avg_price"]) * sell_qty, 2)
    ex["qty"] -= sell_qty
    if ex["qty"] <= 0:
        store.remove_paper_position(symbol)
    else:
        store.upsert_paper_position(ex)
    store.log_paper_trade({"symbol": symbol, "side": "SELL", "qty": sell_qty,
                           "price": round(price, 2), "pnl": realized, "ts": now})
    return {"status": "ok", "mode": "paper", "action": "SELL", "fill": round(price, 2), "realized_pnl": realized}


def _live(symbol: str, side: str, qty: int) -> dict:
    """Place a REAL delivery (CNC) order via core-engine."""
    try:
        r = httpx.post(
            f"{settings.core_engine_url}/fyers/orders/place",
            params={"symbol": symbol, "side": side, "quantity": qty, "product": "CNC"},
            timeout=30.0,
        )
        r.raise_for_status()
        data = r.json()
        logger.info("LIVE %s %d %s placed: %s", side, qty, symbol, data.get("order_id"))
        return {"status": "ok", "mode": "live", "action": side, "order_id": data.get("order_id"), "data": data}
    except httpx.HTTPError as e:
        logger.warning("live order failed %s %s: %s", side, symbol, e)
        return {"status": "error", "mode": "live", "detail": str(e)}


# ── Merged positions ──────────────────────────────────────────────────────────
def _fetch_holdings() -> list[dict]:
    try:
        r = httpx.get(f"{settings.core_engine_url}/fyers/holdings", timeout=20.0)
        r.raise_for_status()
        return r.json().get("holdings", []) or []
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("holdings fetch failed: %s", e)
        return []


def list_positions() -> list[dict]:
    out = []
    for h in _fetch_holdings():
        cost = float(h.get("costPrice") or 0)
        qty = int(h.get("quantity") or 0)
        ltp = float(h.get("ltp") or 0)
        out.append({
            "symbol": h.get("symbol"), "name": (h.get("symbol") or "").split(":")[-1].replace("-EQ", ""),
            "source": "ACTUAL", "qty": qty, "avg_price": round(cost, 2), "ltp": round(ltp, 2),
            "pnl": round(float(h.get("pl") or 0), 0),
            "pnl_pct": round((ltp - cost) / cost * 100, 1) if cost else 0.0,
        })
    for p in store.get_paper_positions():
        ltp = _ltp(p["symbol"])
        avg = p["avg_price"]
        out.append({
            "symbol": p["symbol"], "name": p["name"], "source": "PAPER",
            "qty": p["qty"], "avg_price": avg, "ltp": round(ltp, 2),
            "pnl": round((ltp - avg) * p["qty"], 0),
            "pnl_pct": round((ltp - avg) / avg * 100, 1) if avg else 0.0,
        })
    return out
