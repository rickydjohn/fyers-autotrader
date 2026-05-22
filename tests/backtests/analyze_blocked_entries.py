#!/usr/bin/env python3
"""
End-of-session report on pre-entry-exit-simulation blocks.

The sim engine writes a JSON record to Redis (`blocked_entries:{date}`)
every time the pre-entry exit simulation refuses an entry. This script
reads those records, looks at the underlying's movement over the next
30 minutes, and reports whether each block was:

  - CORRECT     : underlying moved against the would-be trade direction
                  (or didn't move favorably enough) — block prevented a loser
  - MISSED-WIN  : underlying moved favorably by ≥0.20% (≈+10% on ATM option)
                  — block prevented what would have been a winner
  - INCONCLUSIVE: underlying barely moved either way

Run inside trading-data container (Redis access via service name + DB query):
    docker cp tests/backtests/analyze_blocked_entries.py trading-data:/tmp/
    docker exec trading-data python /tmp/analyze_blocked_entries.py [YYYY-MM-DD]

If no date is passed, defaults to today (IST).
"""

import asyncio
import json
import sys
from datetime import date as _date, datetime, timedelta

import asyncpg
import pytz
import redis.asyncio as aioredis

DB_DSN     = "postgresql://trading:trading@timescaledb:5432/trading"
REDIS_URL  = "redis://trading-redis:6379"
IST        = pytz.timezone("Asia/Kolkata")

LOOKFWD_MIN = 30                # how many minutes forward to assess
FAV_THRESHOLD_PCT = 0.0020      # ±0.20% on underlying = ~+10% on ATM option
ADV_THRESHOLD_PCT = 0.0020      # symmetric on the adverse side


def _under_symbol(opt_symbol: str | None, fallback: str) -> str:
    if opt_symbol and "BANKNIFTY" in opt_symbol:
        return "NSE:NIFTYBANK-INDEX"
    if opt_symbol and "NIFTY" in opt_symbol:
        return "NSE:NIFTY50-INDEX"
    return fallback


async def fetch_forward(conn, symbol: str, start_ist, minutes: int):
    end_ist = start_ist + timedelta(minutes=minutes)
    rows = await conn.fetch("""
        SELECT (time AT TIME ZONE 'Asia/Kolkata') AS ts_ist, high, low, close
        FROM market_candles
        WHERE symbol = $1 AND time > $2 AND time <= $3
        ORDER BY time
    """, symbol, start_ist, end_ist)
    return [(r["ts_ist"], float(r["high"]), float(r["low"]), float(r["close"])) for r in rows]


def classify(side: str, entry_under: float, forward) -> tuple:
    """Compute max favorable and adverse moves from entry over the window.
    For BUY: favorable = high > entry; adverse = low < entry.
    For SELL: favorable = low < entry; adverse = high > entry.
    Returns (max_fav_pct, max_adv_pct, verdict, fav_time, adv_time).
    """
    max_fav = 0.0
    max_adv = 0.0
    fav_time = adv_time = None
    for ts, h, l, _c in forward:
        if side == "BUY":
            fav = (h - entry_under) / entry_under
            adv = (entry_under - l) / entry_under
        else:
            fav = (entry_under - l) / entry_under
            adv = (h - entry_under) / entry_under
        if fav > max_fav: max_fav = fav; fav_time = ts
        if adv > max_adv: max_adv = adv; adv_time = ts
    verdict = "INCONCLUSIVE"
    if max_fav >= FAV_THRESHOLD_PCT and max_fav > max_adv:
        verdict = "MISSED-WIN"
    elif max_adv >= ADV_THRESHOLD_PCT:
        verdict = "CORRECT"
    return max_fav, max_adv, verdict, fav_time, adv_time


async def main():
    target_date = sys.argv[1] if len(sys.argv) > 1 else \
                  datetime.now(IST).date().isoformat()
    print(f"Analyzing blocked entries for {target_date}")
    print("=" * 78)

    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    raw = await r.lrange(f"blocked_entries:{target_date}", 0, -1)
    await r.aclose()
    if not raw:
        print(f"No blocked entries recorded for {target_date}.")
        return

    records = [json.loads(x) for x in raw]
    print(f"Total blocked entries: {len(records)}\n")

    conn = await asyncpg.connect(DB_DSN)
    counts = {"CORRECT": 0, "MISSED-WIN": 0, "INCONCLUSIVE": 0}

    for rec in records:
        block_time = datetime.fromisoformat(rec["block_time"])
        if block_time.tzinfo is None:
            block_time = IST.localize(block_time)
        under = _under_symbol(rec.get("option_symbol"), rec["symbol"])
        forward = await fetch_forward(conn, under, block_time, LOOKFWD_MIN)
        if not forward:
            continue
        max_fav, max_adv, verdict, fav_t, adv_t = classify(
            rec["side"], rec["underlying_price"], forward
        )
        counts[verdict] += 1
        t = block_time.strftime("%H:%M:%S")
        sym = rec["symbol"].replace("NSE:", "").replace("-INDEX", "")
        print(f"  {t}  {rec['side']:<5} {sym:<10} "
              f"@ ₹{rec['underlying_price']:.2f}  "
              f"reason={rec['exit_reason']:<22}  "
              f"+{max_fav*100:.2f}% / -{max_adv*100:.2f}%  "
              f"→ {verdict}")

    await conn.close()

    print()
    print("-" * 78)
    n = sum(counts.values())
    if n == 0:
        print("No forward-data trades to classify."); return
    for k in ("CORRECT", "MISSED-WIN", "INCONCLUSIVE"):
        v = counts[k]
        print(f"  {k:<15} {v:>3} / {n}  ({v/n*100:.1f}%)")
    correct_rate = counts["CORRECT"] / (counts["CORRECT"] + counts["MISSED-WIN"]) \
                   if (counts["CORRECT"] + counts["MISSED-WIN"]) > 0 else 0.0
    print(f"\n  Correct-block rate (excluding inconclusive): {correct_rate*100:.1f}%")
    if counts["MISSED-WIN"] > counts["CORRECT"]:
        print(f"  ⚠ More missed-wins than correct blocks — rule may be too aggressive.")
    else:
        print(f"  ✓ Blocks are net protective today.")


if __name__ == "__main__":
    asyncio.run(main())
