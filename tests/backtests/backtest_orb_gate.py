#!/usr/bin/env python3
"""
ORB gate backtest: replay all 164 historical trades to see how many would have
been blocked by the Opening Range Breakout gate, and whether those trades had
worse outcomes than unblocked ones.

Gate logic (as deployed):
  BUY  blocked when underlying price <= orb_high * 1.002
  SELL blocked when underlying price >= orb_low  * 0.998
  Skip gate when orb_high = 0 or orb_low = 0

Data source: TimescaleDB market_candles table (full 1m history since Apr 2025).

Run inside trading-data container:
    docker exec trading-data python /tmp/backtest_orb_gate.py 2>&1
"""

import asyncio
import json
from collections import defaultdict
from datetime import datetime, time as dtime

import asyncpg
import pytz
import redis.asyncio as aioredis
from datetime import date as _date

IST        = pytz.timezone("Asia/Kolkata")
DB_DSN     = "postgresql://trading:trading@timescaledb:5432/trading"
REDIS_URL  = "redis://trading-redis:6379"
import os
ORB_BUFFER = float(os.environ.get("ORB_BUFFER", "0.002"))   # default 0.20% — tunable via env for sweeps


# ── Symbol helpers ────────────────────────────────────────────────────────────

def underlying(option_sym: str) -> str:
    if "BANKNIFTY" in option_sym:
        return "NSE:NIFTYBANK-INDEX"
    return "NSE:NIFTY50-INDEX"


# ── DB queries ────────────────────────────────────────────────────────────────

async def fetch_orb(conn, symbol: str, date_str: str) -> tuple:
    """Return (orb_high, orb_low) from 09:15–09:29 IST candles on date_str."""
    d = _date.fromisoformat(date_str)
    rows = await conn.fetch("""
        SELECT high, low
        FROM market_candles
        WHERE symbol = $1
          AND (time AT TIME ZONE 'Asia/Kolkata')::date = $2
          AND EXTRACT(HOUR   FROM time AT TIME ZONE 'Asia/Kolkata') = 9
          AND EXTRACT(MINUTE FROM time AT TIME ZONE 'Asia/Kolkata') >= 15
          AND EXTRACT(MINUTE FROM time AT TIME ZONE 'Asia/Kolkata') < 30
    """, symbol, d)

    if not rows:
        return 0.0, 0.0
    return (
        float(max(r["high"] for r in rows)),
        float(min(r["low"]  for r in rows)),
    )


async def fetch_underlying_price_at(conn, symbol: str, entry_dt: datetime) -> float | None:
    """Return the close of the 1m candle closest in time to entry_dt."""
    entry_date = entry_dt.date() if entry_dt.tzinfo is None else entry_dt.astimezone(IST).date()
    row = await conn.fetchrow("""
        SELECT close
        FROM market_candles
        WHERE symbol = $1
          AND (time AT TIME ZONE 'Asia/Kolkata')::date = $2
        ORDER BY ABS(EXTRACT(EPOCH FROM (time - $3)))
        LIMIT 1
    """, symbol, entry_date, entry_dt)
    return float(row["close"]) if row else None


# ── Gate logic ────────────────────────────────────────────────────────────────

def would_block(side: str, price: float, orb_high: float, orb_low: float) -> bool:
    if orb_high == 0 or orb_low == 0:
        return False
    if side == "BUY"  and price <= orb_high * (1 + ORB_BUFFER):
        return True
    if side == "SELL" and price >= orb_low  * (1 - ORB_BUFFER):
        return True
    return False


# ── Statistics ────────────────────────────────────────────────────────────────

def stats(trades: list) -> dict:
    if not trades:
        return {"n": 0, "win_rate": 0, "avg_pnl": 0, "avg_pnl_pct": 0, "total_pnl": 0}
    pnls = [t.get("pnl", 0) or 0 for t in trades]
    pcts = [t.get("pnl_pct", 0) or 0 for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    return {
        "n":           len(trades),
        "win_rate":    wins / len(trades) * 100,
        "avg_pnl":     sum(pnls) / len(trades),
        "avg_pnl_pct": sum(pcts) / len(trades),
        "total_pnl":   sum(pnls),
    }


def exit_counts(trades: list) -> dict:
    from collections import Counter
    return dict(Counter(t.get("exit_reason") or "?" for t in trades).most_common())


# ── Main ──────────────────────────────────────────────────────────────────────

async def run():
    log("=" * 70)
    log("  ORB GATE BACKTEST")
    log(f"  Buffer: ±{ORB_BUFFER*100:.2f}%   BUY must clear ORB-high, SELL must clear ORB-low")
    log("=" * 70)

    redis_client = aioredis.from_url(REDIS_URL)
    raw = await redis_client.hgetall("trades:all")
    all_trades = [json.loads(v) for v in raw.values()]
    trades = [t for t in all_trades if t.get("status") in ("CLOSED", "STOPPED")]
    log(f"\n  Trades with PnL: {len(trades)}  ({len(all_trades)} total in Redis)")

    conn = await asyncpg.connect(DB_DSN)
    try:
        blocked, passed, skipped = [], [], []
        detail_rows = []

        for t in trades:
            et = t.get("entry_time") or ""
            if not et:
                skipped.append(t); continue

            date_str = et[:10]
            und      = underlying(t.get("symbol") or "")
            side     = t.get("side") or "BUY"

            entry_dt = datetime.fromisoformat(et)
            if entry_dt.tzinfo is None:
                entry_dt = IST.localize(entry_dt)

            price    = await fetch_underlying_price_at(conn, und, entry_dt)
            if price is None:
                skipped.append(t); continue

            orb_high, orb_low = await fetch_orb(conn, und, date_str)

            blocked_flag = would_block(side, price, orb_high, orb_low)
            bucket = "BLOCKED" if blocked_flag else ("NO_ORB" if orb_high == 0 else "PASSED")

            detail_rows.append({
                "date":     date_str,
                "time":     entry_dt.astimezone(IST).strftime("%H:%M"),
                "und":      und.replace("NSE:", "")[:20],
                "side":     side,
                "price":    price,
                "orb_h":    orb_high,
                "orb_l":    orb_low,
                "pnl":      t.get("pnl") or 0,
                "pnl_pct":  t.get("pnl_pct") or 0,
                "exit":     t.get("exit_reason") or "?",
                "bucket":   bucket,
            })

            if blocked_flag:
                blocked.append(t)
            elif orb_high == 0:
                skipped.append(t)
            else:
                passed.append(t)

    finally:
        await conn.close()

    # ── Detail table ──────────────────────────────────────────────────────────
    log(f"\n  {'Date':<12} {'Time':<6} {'Side':<5} {'Price':>8} {'ORB-H':>8} {'ORB-L':>8}  {'PnL':>8}  {'PnL%':>6}  {'Exit':<16}  Bucket")
    log(f"  {'-'*100}")
    for r in sorted(detail_rows, key=lambda x: (x["date"], x["time"])):
        flag = " ← BLOCKED" if r["bucket"] == "BLOCKED" else ""
        log(
            f"  {r['date']:<12} {r['time']:<6} {r['side']:<5} "
            f"{r['price']:>8.0f} {r['orb_h']:>8.0f} {r['orb_l']:>8.0f}  "
            f"{r['pnl']:>+8.0f}  {r['pnl_pct']:>+5.1f}%  "
            f"{r['exit']:<16}{flag}"
        )

    # ── Summary ───────────────────────────────────────────────────────────────
    log(f"\n{'='*70}")
    log("  SUMMARY")
    log(f"{'='*70}")

    for label, bucket in [
        ("BLOCKED  (would have been filtered out)", blocked),
        ("PASSED   (would have been traded)",       passed),
        ("SKIPPED  (no ORB data / no price data)",  skipped),
    ]:
        s = stats(bucket)
        log(f"\n  {label}")
        log(f"    Count       : {s['n']}")
        if s["n"]:
            log(f"    Win rate    : {s['win_rate']:>6.1f}%")
            log(f"    Avg PnL     : ₹{s['avg_pnl']:>+8.0f}  ({s['avg_pnl_pct']:>+.2f}%)")
            log(f"    Total PnL   : ₹{s['total_pnl']:>+,.0f}")
            log(f"    Exit reasons: {exit_counts(bucket)}")

    valid = len(blocked) + len(passed)
    if valid:
        log(f"\n  Gate would block {len(blocked)}/{valid} trades ({len(blocked)/valid*100:.1f}%)")

    # ── Daily breakdown ───────────────────────────────────────────────────────
    log(f"\n  {'Date':<12} {'Total':>6} {'Blocked':>8} {'Passed':>7}  PnL if blocked      PnL if passed")
    log(f"  {'-'*72}")
    by_date = defaultdict(lambda: {"bl": [], "pa": []})
    for r in detail_rows:
        if r["bucket"] == "BLOCKED":
            by_date[r["date"]]["bl"].append(r["pnl"])
        elif r["bucket"] == "PASSED":
            by_date[r["date"]]["pa"].append(r["pnl"])

    for date in sorted(by_date):
        bl = by_date[date]["bl"]
        pa = by_date[date]["pa"]
        total = len(bl) + len(pa)
        bl_str = f"₹{sum(bl):>+,.0f} ({len(bl)} tr)" if bl else "—"
        pa_str = f"₹{sum(pa):>+,.0f} ({len(pa)} tr)" if pa else "—"
        log(f"  {date:<12} {total:>6} {len(bl):>8} {len(pa):>7}  {bl_str:<22}  {pa_str}")


def log(msg: str) -> None:
    print(msg, flush=True)


if __name__ == "__main__":
    asyncio.run(run())
