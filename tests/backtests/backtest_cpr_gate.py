#!/usr/bin/env python3
"""
CPR gate backtest: replay all historical closed/stopped trades to see how many
would have been blocked by the CPR band gate, and whether those trades had
worse outcomes than unblocked ones.

Gate logic (as deployed):
  upper = max(CPR_TC, CPR_BC)   — handles both normal and inverted CPR
  lower = min(CPR_TC, CPR_BC)

  BUY  blocked when price <= upper * 1.002  (not confirmed above band)
  SELL blocked when price >= lower * 0.998  (not confirmed below band)

  Gate skipped when TC=0 or BC=0 (no data for that date).

CPR source: daily_indicators table (tc, bc columns), keyed by (date, symbol).
Price source: market_candles 1m close nearest to entry_time.

Run inside trading-data container:
    docker exec trading-data python /tmp/backtest_cpr_gate.py 2>&1
"""

import asyncio
import json
from collections import defaultdict
from datetime import datetime
from datetime import date as _date

import asyncpg
import pytz
import redis.asyncio as aioredis

IST        = pytz.timezone("Asia/Kolkata")
DB_DSN     = "postgresql://trading:trading@timescaledb:5432/trading"
REDIS_URL  = "redis://trading-redis:6379"
CPR_BUFFER = 0.002   # 0.20% — matches deployed gate


# ── Symbol helpers ────────────────────────────────────────────────────────────

def underlying(option_sym: str) -> str:
    if "BANKNIFTY" in option_sym:
        return "NSE:NIFTYBANK-INDEX"
    return "NSE:NIFTY50-INDEX"


# ── DB queries ────────────────────────────────────────────────────────────────

async def fetch_cpr(conn, symbol: str, date_str: str) -> tuple:
    """Return (tc, bc) from daily_indicators for the given symbol+date."""
    d = _date.fromisoformat(date_str)
    row = await conn.fetchrow(
        "SELECT tc, bc FROM daily_indicators WHERE symbol=$1 AND date=$2",
        symbol, d,
    )
    if not row:
        return 0.0, 0.0
    return float(row["tc"]), float(row["bc"])


async def fetch_underlying_price_at(conn, symbol: str, entry_dt: datetime) -> float | None:
    """Return the close of the 1m candle closest in time to entry_dt."""
    entry_date = entry_dt.astimezone(IST).date()
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

def would_block(side: str, price: float, tc: float, bc: float) -> bool:
    if tc == 0 or bc == 0:
        return False
    upper = max(tc, bc)
    lower = min(tc, bc)
    if side == "BUY"  and price <= upper * (1 + CPR_BUFFER):
        return True
    if side == "SELL" and price >= lower * (1 - CPR_BUFFER):
        return True
    return False


def cpr_zone(price: float, tc: float, bc: float) -> str:
    """Describe where price sits relative to the CPR band."""
    if tc == 0 or bc == 0:
        return "NO_DATA"
    upper = max(tc, bc)
    lower = min(tc, bc)
    buf_u = upper * (1 + CPR_BUFFER)
    buf_l = lower * (1 - CPR_BUFFER)
    if price < lower:
        return "BELOW_BAND"
    elif price <= buf_l:
        return "INSIDE_LOWER_BUF"
    elif price < upper:
        return "INSIDE_BAND"
    elif price <= buf_u:
        return "INSIDE_UPPER_BUF"
    else:
        return "ABOVE_BAND"


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
    log("=" * 72)
    log("  CPR GATE BACKTEST")
    log(f"  Buffer: ±{CPR_BUFFER*100:.2f}%   upper=max(TC,BC)  lower=min(TC,BC)")
    log(f"  BUY  blocked when price <= upper*(1+{CPR_BUFFER})  — not confirmed above band")
    log(f"  SELL blocked when price >= lower*(1-{CPR_BUFFER})  — not confirmed below band")
    log("=" * 72)

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
                skipped.append(t)
                continue

            date_str = et[:10]
            und      = underlying(t.get("symbol") or "")
            side     = t.get("side") or "BUY"

            entry_dt = datetime.fromisoformat(et)
            if entry_dt.tzinfo is None:
                entry_dt = IST.localize(entry_dt)

            price = await fetch_underlying_price_at(conn, und, entry_dt)
            if price is None:
                skipped.append(t)
                continue

            tc, bc = await fetch_cpr(conn, und, date_str)

            blocked_flag = would_block(side, price, tc, bc)
            if tc == 0 or bc == 0:
                bucket = "NO_CPR"
            else:
                bucket = "BLOCKED" if blocked_flag else "PASSED"

            upper = max(tc, bc) if tc and bc else 0
            lower = min(tc, bc) if tc and bc else 0
            clearance = (
                ((price / upper) - 1) * 100 if side == "BUY" and upper > 0
                else ((lower / price) - 1) * 100 if side == "SELL" and lower > 0
                else 0.0
            )

            detail_rows.append({
                "date":      date_str,
                "time":      entry_dt.astimezone(IST).strftime("%H:%M"),
                "und":       und.replace("NSE:", "")[:18],
                "side":      side,
                "price":     price,
                "tc":        tc,
                "bc":        bc,
                "upper":     upper,
                "lower":     lower,
                "clear_pct": clearance,
                "zone":      cpr_zone(price, tc, bc),
                "pnl":       t.get("pnl") or 0,
                "pnl_pct":   t.get("pnl_pct") or 0,
                "exit":      t.get("exit_reason") or "?",
                "bucket":    bucket,
            })

            if bucket == "BLOCKED":
                blocked.append(t)
            elif bucket == "NO_CPR":
                skipped.append(t)
            else:
                passed.append(t)

    finally:
        await conn.close()

    # ── Detail table ──────────────────────────────────────────────────────────
    log(f"\n  {'Date':<12} {'Time':<6} {'Side':<5} {'Price':>8} {'Upper':>8} {'Lower':>8}  {'Clear%':>7}  {'PnL':>8}  {'Exit':<18}  {'Zone':<20}  Bucket")
    log(f"  {'-'*115}")
    for r in sorted(detail_rows, key=lambda x: (x["date"], x["time"])):
        flag = " ← BLOCKED" if r["bucket"] == "BLOCKED" else ""
        log(
            f"  {r['date']:<12} {r['time']:<6} {r['side']:<5} "
            f"{r['price']:>8.0f} {r['upper']:>8.0f} {r['lower']:>8.0f}  "
            f"{r['clear_pct']:>+6.2f}%  "
            f"{r['pnl']:>+8.0f}  "
            f"{r['exit']:<18}  "
            f"{r['zone']:<20}{flag}"
        )

    # ── Summary ───────────────────────────────────────────────────────────────
    log(f"\n{'='*72}")
    log("  SUMMARY")
    log(f"{'='*72}")

    for label, bucket in [
        ("BLOCKED  (would have been filtered out)", blocked),
        ("PASSED   (would have been traded)",       passed),
        ("SKIPPED  (no CPR data / no price data)",  skipped),
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

    # ── Clearance distribution for blocked trades ─────────────────────────────
    if blocked:
        log(f"\n{'='*72}")
        log("  CLEARANCE BREAKDOWN — BLOCKED TRADES")
        log(f"{'='*72}")
        log(f"  (clearance = how far price was from the CPR boundary at entry)")
        bins = {"<-2%": [], "-2% to -1%": [], "-1% to 0%": [], "0% to +0.1%": [], "+0.1% to +0.2%": []}
        for r in detail_rows:
            if r["bucket"] != "BLOCKED":
                continue
            c = r["clear_pct"]
            if c < -2:
                bins["<-2%"].append(r)
            elif c < -1:
                bins["-2% to -1%"].append(r)
            elif c < 0:
                bins["-1% to 0%"].append(r)
            elif c < 0.1:
                bins["0% to +0.1%"].append(r)
            else:
                bins["+0.1% to +0.2%"].append(r)
        for label, rows in bins.items():
            if not rows:
                continue
            pnls = [r["pnl"] for r in rows]
            wins = sum(1 for p in pnls if p > 0)
            log(f"  {label:<18}  {len(rows):>3} trades  "
                f"win={wins/len(rows)*100:>5.1f}%  "
                f"avg=₹{sum(pnls)/len(rows):>+,.0f}  "
                f"total=₹{sum(pnls):>+,.0f}")

    # ── Daily breakdown ───────────────────────────────────────────────────────
    log(f"\n{'='*72}")
    log("  DAILY BREAKDOWN")
    log(f"{'='*72}")
    log(f"  {'Date':<12} {'Total':>6} {'Blocked':>8} {'Passed':>7}  PnL if blocked      PnL if passed")
    log(f"  {'-'*75}")
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
