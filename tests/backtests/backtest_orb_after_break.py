#!/usr/bin/env python3
"""
ORB-after-break backtest.

Question being answered:
  Once the ORB range is broken (either direction) at any point after 09:30 IST,
  does the market continue to respect the ORB boundary, or does it stop
  treating ORB as relevant for the rest of the day?

For each (symbol, trading-day):
  1. Compute ORB high/low from 09:15-09:29 IST 1m bars.
  2. Compute buffered thresholds:  th_high = orb_high * 1.002,  th_low = orb_low * 0.998.
  3. Walk 1m bars from 09:30 → 15:30 IST; find FIRST 1m close that crosses a threshold.
  4. From that first-break time onward, classify the rest-of-day behavior:
       - CLEAN_CONT     : close never returns inside the raw ORB range
       - MEAN_REVERTED  : close returns inside ORB and stays (no further break)
       - SAME_AGAIN     : returns inside ORB, then breaks same side again
       - REVERSAL       : returns inside ORB, then breaks the OPPOSITE threshold
  5. Track: max post-break extension in primary direction (favorable move),
            max post-break drawdown back through / past ORB (adverse move).

If the user's proposed rule ("after first ORB break, stop using ORB as a blocker")
were applied, the REVERSAL bucket is the danger zone — those are days where a
post-break opposite-direction signal would NOT have been chasing a real move.

Run inside trading-data container:
    docker cp tests/backtests/backtest_orb_after_break.py trading-data:/tmp/
    docker exec trading-data python /tmp/backtest_orb_after_break.py
"""

import asyncio
from collections import defaultdict
from datetime import date as _date, time as dtime

import asyncpg
import pytz

DB_DSN = "postgresql://trading:trading@timescaledb:5432/trading"
IST    = pytz.timezone("Asia/Kolkata")

ORB_BUFFER = 0.002    # 0.20% (matches deployed gate)

SYMBOLS = ["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX"]


async def fetch_session_bars(conn, symbol: str, day: _date):
    """1m bars for the full session 09:15-15:30 IST on `day`, sorted by time."""
    rows = await conn.fetch("""
        SELECT (time AT TIME ZONE 'Asia/Kolkata') AS ts_ist,
               open, high, low, close
        FROM market_candles
        WHERE symbol = $1
          AND (time AT TIME ZONE 'Asia/Kolkata')::date = $2
          AND EXTRACT(HOUR FROM time AT TIME ZONE 'Asia/Kolkata') BETWEEN 9 AND 15
        ORDER BY time
    """, symbol, day)
    return [dict(r) for r in rows]


def split_orb(bars):
    """Return (orb_bars, post_bars). orb = 09:15-09:29, post = 09:30-15:30."""
    orb, post = [], []
    for b in bars:
        t = b["ts_ist"].time()
        if dtime(9, 15) <= t < dtime(9, 30):
            orb.append(b)
        elif dtime(9, 30) <= t <= dtime(15, 30):
            post.append(b)
    return orb, post


def classify_day(orb_high, orb_low, post_bars):
    """
    Returns dict with:
      bucket: NEVER_BROKE | CLEAN_CONT | MEAN_REVERTED | SAME_AGAIN | REVERSAL
      first_break_time, first_break_dir, first_break_price
      first_extension_pct  (peak in primary direction, from first-break price)
      reverse_swing_pct    (peak-to-trough swing across the reversal, REVERSAL only)
    """
    th_high = orb_high * (1 + ORB_BUFFER)
    th_low  = orb_low  * (1 - ORB_BUFFER)

    # 1. find first break
    first_idx = None
    first_dir = None
    for i, b in enumerate(post_bars):
        if b["close"] > th_high:
            first_idx, first_dir = i, "UP"
            break
        if b["close"] < th_low:
            first_idx, first_dir = i, "DOWN"
            break

    if first_idx is None:
        return {"bucket": "NEVER_BROKE"}

    first_bar = post_bars[first_idx]
    first_price = first_bar["close"]

    # 2. walk post-break bars and track peak in first direction
    returned_inside  = False
    broke_same_again = False
    broke_other      = False
    other_break_idx  = None

    peak_in_first_dir = first_price
    peak_idx          = first_idx

    max_fav = 0.0
    max_adv = 0.0

    for i, b in enumerate(post_bars[first_idx + 1:], start=first_idx + 1):
        # extension tracking
        if first_dir == "UP":
            fav = (b["high"] - first_price) / first_price
            adv = (first_price - b["low"]) / first_price
            if b["high"] > peak_in_first_dir:
                peak_in_first_dir = b["high"]
                peak_idx          = i
        else:
            fav = (first_price - b["low"]) / first_price
            adv = (b["high"] - first_price) / first_price
            if b["low"] < peak_in_first_dir:
                peak_in_first_dir = b["low"]
                peak_idx          = i
        max_fav = max(max_fav, fav)
        max_adv = max(max_adv, adv)

        # behavioral classification (close-based, like the gate)
        close = b["close"]
        if orb_low <= close <= orb_high:
            returned_inside = True
        if returned_inside:
            if first_dir == "UP" and close < th_low:
                broke_other     = True
                other_break_idx = i
                break
            if first_dir == "DOWN" and close > th_high:
                broke_other     = True
                other_break_idx = i
                break
            if (first_dir == "UP"   and close > th_high) or \
               (first_dir == "DOWN" and close < th_low):
                broke_same_again = True

    if not returned_inside:
        bucket = "CLEAN_CONT"
    elif broke_other:
        bucket = "REVERSAL"
    elif broke_same_again:
        bucket = "SAME_AGAIN"
    else:
        bucket = "MEAN_REVERTED"

    result = {
        "bucket": bucket,
        "first_break_time": first_bar["ts_ist"].time().isoformat(timespec="minutes"),
        "first_break_dir": first_dir,
        "first_break_price": first_price,
        "max_favorable_pct": max_fav,
        "max_adverse_pct": max_adv,
        "orb_high": orb_high,
        "orb_low":  orb_low,
    }

    # extra detail for REVERSAL days
    if bucket == "REVERSAL":
        # peak in first direction
        peak_bar = post_bars[peak_idx]
        peak_t   = peak_bar["ts_ist"].time().isoformat(timespec="minutes")
        first_ext = (peak_in_first_dir - first_price) / first_price
        if first_dir == "DOWN":
            first_ext = -first_ext

        # extreme in opposite direction AFTER crossing other threshold
        # (search from other_break_idx onward for the furthest move)
        opp_extreme = post_bars[other_break_idx]["close"]
        opp_idx     = other_break_idx
        for i, b in enumerate(post_bars[other_break_idx:], start=other_break_idx):
            if first_dir == "UP" and b["low"] < opp_extreme:
                opp_extreme = b["low"]; opp_idx = i
            elif first_dir == "DOWN" and b["high"] > opp_extreme:
                opp_extreme = b["high"]; opp_idx = i
        opp_t = post_bars[opp_idx]["ts_ist"].time().isoformat(timespec="minutes")

        # full swing: from peak in first dir to extreme in opposite dir
        swing = (peak_in_first_dir - opp_extreme) / peak_in_first_dir
        if first_dir == "DOWN":
            swing = -swing

        result.update({
            "peak_in_first_dir":       peak_in_first_dir,
            "peak_time":               peak_t,
            "first_extension_pct":     abs(first_ext),
            "opposite_extreme":        opp_extreme,
            "opposite_extreme_time":   opp_t,
            "reverse_swing_pct":       abs(swing),
        })

    return result


async def main():
    conn = await asyncpg.connect(DB_DSN)

    # all trading dates with data for both symbols
    dates = await conn.fetch("""
        SELECT DISTINCT (time AT TIME ZONE 'Asia/Kolkata')::date AS d
        FROM market_candles
        WHERE symbol = ANY($1::text[])
          AND EXTRACT(HOUR FROM time AT TIME ZONE 'Asia/Kolkata') BETWEEN 9 AND 15
        ORDER BY d
    """, SYMBOLS)
    dates = [r["d"] for r in dates]

    summary = {sym: defaultdict(list) for sym in SYMBOLS}    # sym -> bucket -> [results]

    for d in dates:
        for sym in SYMBOLS:
            bars = await fetch_session_bars(conn, sym, d)
            if len(bars) < 30:
                continue
            orb_bars, post_bars = split_orb(bars)
            if not orb_bars or not post_bars:
                continue
            # cast Decimal → float once at the boundary
            for b in bars:
                for k in ("open", "high", "low", "close"):
                    b[k] = float(b[k])
            orb_high = max(b["high"] for b in orb_bars)
            orb_low  = min(b["low"]  for b in orb_bars)
            result = classify_day(orb_high, orb_low, post_bars)
            result["date"] = d.isoformat()
            result["orb_pct"] = (orb_high - orb_low) / orb_low * 100
            summary[sym][result["bucket"]].append(result)

    await conn.close()

    # ── report ────────────────────────────────────────────────────────────────
    print("=" * 78)
    print(f"ORB-after-break backtest    buffer={ORB_BUFFER*100:.2f}%")
    print(f"Date range: {dates[0]} → {dates[-1]}    total days: {len(dates)}")
    print("=" * 78)

    for sym in SYMBOLS:
        total = sum(len(v) for v in summary[sym].values())
        if total == 0:
            continue
        print(f"\n{sym}    n={total}")
        print("-" * 78)
        order = ["CLEAN_CONT", "MEAN_REVERTED", "SAME_AGAIN", "REVERSAL", "NEVER_BROKE"]
        for bucket in order:
            rows = summary[sym].get(bucket, [])
            pct = 100.0 * len(rows) / total if total else 0.0
            print(f"  {bucket:<16}  {len(rows):>4} days   {pct:5.1f}%")
            if rows and bucket != "NEVER_BROKE":
                fav = sorted(r["max_favorable_pct"] for r in rows)
                adv = sorted(r["max_adverse_pct"]   for r in rows)
                p = lambda xs, q: xs[int(len(xs) * q)] if xs else 0.0
                print(f"      post-break favorable (median / p75 / p90):  "
                      f"{p(fav,0.5)*100:.2f}% / {p(fav,0.75)*100:.2f}% / {p(fav,0.90)*100:.2f}%")
                print(f"      post-break adverse   (median / p75 / p90):  "
                      f"{p(adv,0.5)*100:.2f}% / {p(adv,0.75)*100:.2f}% / {p(adv,0.90)*100:.2f}%")

        # Key answer to the user's question:
        broke_days = total - len(summary[sym].get("NEVER_BROKE", []))
        if broke_days:
            clean  = len(summary[sym].get("CLEAN_CONT",    []))
            rev    = len(summary[sym].get("REVERSAL",      []))
            same   = len(summary[sym].get("SAME_AGAIN",    []))
            mean_r = len(summary[sym].get("MEAN_REVERTED", []))
            print(f"\n  Of days where ORB was broken (n={broke_days}):")
            print(f"    Clean continuation (never returned inside ORB) : {clean/broke_days*100:5.1f}%")
            print(f"    Mean-reverted (returned, no further break)     : {mean_r/broke_days*100:5.1f}%")
            print(f"    Broke same side again (whipsaw, then trend)    : {same/broke_days*100:5.1f}%")
            print(f"    REVERSAL (broke opposite threshold)            : {rev/broke_days*100:5.1f}%   ← danger for user's proposed rule")

        # ── REVERSAL detail dump ──
        rev_rows = sorted(summary[sym].get("REVERSAL", []), key=lambda r: r["date"])
        if rev_rows:
            print(f"\n  REVERSAL day detail ({len(rev_rows)} days):")
            print(f"    {'date':<12} {'dir':<4} {'first_break':<12} {'1st-ext%':<9} "
                  f"{'peak_time':<10} {'rev_swing%':<11} {'opp_extreme_time'}")
            firsts = []; swings = []
            for r in rev_rows:
                firsts.append(r["first_extension_pct"])
                swings.append(r["reverse_swing_pct"])
                print(f"    {r['date']:<12} {r['first_break_dir']:<4} "
                      f"{r['first_break_time']:<12} "
                      f"{r['first_extension_pct']*100:>7.2f}%  "
                      f"{r['peak_time']:<10} "
                      f"{r['reverse_swing_pct']*100:>9.2f}%   "
                      f"{r['opposite_extreme_time']}")
            firsts.sort(); swings.sort()
            p = lambda xs, q: xs[int(len(xs) * q)] if xs else 0.0
            print(f"\n    1st-direction extension  (median / p75 / p90):  "
                  f"{p(firsts,0.5)*100:.2f}% / {p(firsts,0.75)*100:.2f}% / {p(firsts,0.90)*100:.2f}%")
            print(f"    Reverse swing magnitude  (median / p75 / p90):  "
                  f"{p(swings,0.5)*100:.2f}% / {p(swings,0.75)*100:.2f}% / {p(swings,0.90)*100:.2f}%")


if __name__ == "__main__":
    asyncio.run(main())
