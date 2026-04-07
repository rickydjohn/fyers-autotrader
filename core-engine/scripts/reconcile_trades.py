"""
One-off trade reconciliation script.

For each closed trade:
  1. Fetch 1m candles from Fyers for the trade date and symbol.
  2. Find the candle covering entry_time and exit_time.
  3. If the stored price falls outside the candle's high/low, replace it with
     the candle close (best single-point estimate of price that minute).
  4. Recalculate pnl and pnl_pct from the (possibly corrected) prices.
  5. PATCH the trade record in data-service via the ingest upsert endpoint.

Run inside trading-core:
  docker exec trading-core python3 /app/scripts/reconcile_trades.py
"""

import sys, os
sys.path.insert(0, "/app")

import json
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import httpx
import pytz

from fyers.auth import get_fyers_client
from fyers.market_data import get_historical_candles_daterange

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
log = logging.getLogger("reconcile")

IST         = pytz.timezone("Asia/Kolkata")
DATA_URL    = os.environ.get("DATA_SERVICE_URL", "http://data-service:8003")
COMMISSION  = 20.0   # per leg, flat


# ── Fetch trades from data-service ───────────────────────────────────────────

def fetch_trades(month: str = "2026-04") -> List[dict]:
    resp = httpx.get(f"{DATA_URL}/api/v1/report/trades", params={"month": month}, timeout=15)
    resp.raise_for_status()
    return resp.json().get("trades", [])


# ── Fetch 1m candles from Fyers ───────────────────────────────────────────────

_candle_cache: Dict[str, List] = {}

def get_candles_for_date(symbol: str, date_str: str) -> List:
    key = f"{symbol}:{date_str}"
    if key in _candle_cache:
        return _candle_cache[key]
    bars = get_historical_candles_daterange(symbol, "1m", date_str, date_str)
    _candle_cache[key] = bars
    return bars


def find_candle(bars: List, ts: datetime) -> Optional[object]:
    """Return the 1m candle whose window contains ts (candle open ≤ ts < candle open + 60s)."""
    if not bars:
        return None
    ts_aware = ts if ts.tzinfo else IST.localize(ts)
    # bars are sorted ascending; find the last candle whose timestamp <= ts
    best = None
    for bar in bars:
        bar_ts = bar.timestamp if bar.timestamp.tzinfo else IST.localize(bar.timestamp)
        if bar_ts <= ts_aware:
            best = bar
        else:
            break
    return best


def price_in_range(price: float, bar) -> bool:
    """Return True if price is within the candle's high/low (with 0.5% tolerance)."""
    tol = bar.high * 0.005
    return (bar.low - tol) <= price <= (bar.high + tol)


# ── Recalculate one trade ─────────────────────────────────────────────────────

def reconcile(trade: dict, candles: List) -> Optional[dict]:
    """
    Returns an updated trade dict if any field changed, else None.
    """
    entry_ts = datetime.fromisoformat(trade["entry_time"])
    exit_ts  = datetime.fromisoformat(trade["exit_time"])
    side     = trade["side"]
    qty      = trade["quantity"]
    old_entry  = float(trade["entry_price"])
    old_exit   = float(trade["exit_price"])
    commission = float(trade.get("commission") or COMMISSION * 2)

    entry_bar = find_candle(candles, entry_ts)
    exit_bar  = find_candle(candles, exit_ts)

    new_entry = old_entry
    new_exit  = old_exit
    changed   = False

    if entry_bar:
        if not price_in_range(old_entry, entry_bar):
            log.warning(
                f"  [ENTRY MISMATCH] {trade['symbol']} @ {entry_ts.strftime('%H:%M:%S')} "
                f"stored=₹{old_entry:.2f}  candle=[₹{entry_bar.low:.2f}–₹{entry_bar.high:.2f}] "
                f"→ using close ₹{entry_bar.close:.2f}"
            )
            new_entry = entry_bar.close
            changed = True
        else:
            log.info(
                f"  [ENTRY OK]      {trade['symbol']} @ {entry_ts.strftime('%H:%M:%S')} "
                f"₹{old_entry:.2f} ∈ [₹{entry_bar.low:.2f}–₹{entry_bar.high:.2f}]"
            )
    else:
        log.warning(f"  [NO CANDLE] {trade['symbol']} entry {entry_ts.strftime('%H:%M:%S')} — no Fyers data")

    if exit_bar:
        if not price_in_range(old_exit, exit_bar):
            log.warning(
                f"  [EXIT  MISMATCH] {trade['symbol']} @ {exit_ts.strftime('%H:%M:%S')} "
                f"stored=₹{old_exit:.2f}  candle=[₹{exit_bar.low:.2f}–₹{exit_bar.high:.2f}] "
                f"→ using close ₹{exit_bar.close:.2f}"
            )
            new_exit = exit_bar.close
            changed = True
        else:
            log.info(
                f"  [EXIT  OK]      {trade['symbol']} @ {exit_ts.strftime('%H:%M:%S')} "
                f"₹{old_exit:.2f} ∈ [₹{exit_bar.low:.2f}–₹{exit_bar.high:.2f}]"
            )
    else:
        log.warning(f"  [NO CANDLE] {trade['symbol']} exit  {exit_ts.strftime('%H:%M:%S')} — no Fyers data")

    if not changed:
        return None

    if side == "BUY":
        gross_pnl = (new_exit - new_entry) * qty
    else:
        gross_pnl = (new_entry - new_exit) * qty

    net_pnl   = round(gross_pnl - commission, 2)
    invested  = new_entry * qty
    pnl_pct   = round(net_pnl / invested * 100, 3) if invested else 0.0

    updated = dict(trade)
    updated["entry_price"] = round(new_entry, 2)
    updated["exit_price"]  = round(new_exit,  2)
    updated["pnl"]         = net_pnl
    updated["pnl_pct"]     = pnl_pct
    return updated


# ── Persist update ────────────────────────────────────────────────────────────

def push_update(trade: dict) -> None:
    resp = httpx.post(f"{DATA_URL}/api/v1/ingest/trade", json=trade, timeout=10)
    resp.raise_for_status()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("Fetching trades from data-service…")
    trades = fetch_trades("2026-04")
    closed = [t for t in trades if t.get("status") in ("CLOSED", "STOPPED")
              and t.get("exit_price") is not None]
    log.info(f"Found {len(closed)} closed trades to reconcile")

    # Group by (symbol, date) to minimise Fyers API calls
    groups: Dict[Tuple[str, str], List[dict]] = {}
    for t in closed:
        symbol    = t.get("option_symbol") or t["symbol"]
        date_str  = datetime.fromisoformat(t["entry_time"]).strftime("%Y-%m-%d")
        groups.setdefault((symbol, date_str), []).append(t)

    updated_count  = 0
    verified_count = 0
    no_data_count  = 0

    for (symbol, date_str), group in groups.items():
        log.info(f"\n── {symbol}  {date_str}  ({len(group)} trades) ──")
        try:
            candles = get_candles_for_date(symbol, date_str)
        except Exception as e:
            log.error(f"  Fyers fetch failed: {e}")
            no_data_count += len(group)
            continue

        if not candles:
            log.warning(f"  No 1m candle data returned from Fyers for {symbol}")
            no_data_count += len(group)
            continue

        log.info(f"  Fetched {len(candles)} 1m candles "
                 f"({candles[0].timestamp.strftime('%H:%M')}–{candles[-1].timestamp.strftime('%H:%M')})")

        for trade in group:
            result = reconcile(trade, candles)
            if result:
                push_update(result)
                log.info(
                    f"  ✓ UPDATED  pnl: ₹{trade.get('pnl', '?')} → ₹{result['pnl']}  "
                    f"pnl_pct: {trade.get('pnl_pct', '?')}% → {result['pnl_pct']}%"
                )
                updated_count += 1
            else:
                verified_count += 1

    log.info(
        f"\n{'─'*60}\n"
        f"Reconciliation complete:\n"
        f"  ✓ Verified (prices correct): {verified_count}\n"
        f"  ✏ Updated  (prices fixed):   {updated_count}\n"
        f"  ✗ No Fyers data:             {no_data_count}\n"
        f"  Total processed:             {len(closed)}"
    )


if __name__ == "__main__":
    main()
