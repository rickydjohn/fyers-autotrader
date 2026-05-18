#!/usr/bin/env python3
"""
Backtest: Variant 1 of the ORB-after-break rule
─────────────────────────────────────────────────
Current rule (shipped on feat/ws-drift-veto 9c377c5, live since 2026-05-18):
  Once today's ORB is broken in either direction, the gate is disabled
  for BOTH directions for the rest of the day.

Variant 1 — direction-aligned-only relaxation (NOT LIVE, kept as option):
  Once today's ORB is broken in direction D, the gate is disabled ONLY
  for signals in direction D. Opposite-direction signals still need to
  satisfy the original ORB gate (i.e. wait until price crosses the
  opposite threshold).

2026-05-18 verdict: current rule wins on REVERSAL days (23.6% win-rate
vs 5.3% for variant 1) because the LLM does flip direction on many
reversal days and those late entries are profitable. Variant 1 wins on
CLEAN_CONT and SAME_AGAIN buckets. Overall PnL is roughly tied. Decision
was to keep the current rule live and revisit if the LLM stops catching
reversals well in production.

To switch to variant 1 later (single-file change in simulation-engine/main.py):
  In `_is_orb_broken_today(...)`, return not just bool but the direction
  (e.g. "UP" / "DOWN" / None). Then in the ORB gate block, pass the
  decision side and only short-circuit if direction matches side.

The question this backtest answered: does variant 1 cut REVERSAL-day
losses without giving up too many legitimate follow-through entries?

For each (symbol, trading-day) since 2025-10-13:
  1. Compute ORB high/low and first-break direction D1 (post 09:30).
  2. Pull all LLM BUY/SELL decisions for that (symbol, date) AFTER
     first-break time.
  3. For each decision, determine:
       CURRENT  rule outcome:  taken (gate is disabled).
       VARIANT  rule outcome:  taken if signal.direction == D1, else
                               needs price already past the opposite
                               threshold to be taken.
       LEGACY  rule outcome:   taken only if price was already past the
                               corresponding threshold at decision time
                               (no break-amnesty at all).
  4. For each taken signal, evaluate the trade outcome by walking 1m
     bars forward with a fixed SL / TGT pair on the *underlying*:
         BUY:  SL = entry × (1 − 0.30%),  TGT = entry × (1 + 0.60%)
         SELL: SL = entry × (1 + 0.30%),  TGT = entry × (1 − 0.60%)
     End of session = TIMEOUT.

Outcome buckets reported per rule variant, split by the day's behavioral
classification (CLEAN_CONT / SAME_AGAIN / MEAN_REVERTED / REVERSAL).

Run inside trading-data container:
    docker cp tests/backtests/backtest_orb_break_direction_aligned.py trading-data:/tmp/
    docker exec trading-data python /tmp/backtest_orb_break_direction_aligned.py
"""

import asyncio
import json
from collections import defaultdict
from datetime import date as _date, datetime, time as dtime

import asyncpg
import pytz

DB_DSN  = "postgresql://trading:trading@timescaledb:5432/trading"
IST     = pytz.timezone("Asia/Kolkata")

ORB_BUFFER = 0.002
SL_PCT     = 0.003
TGT_PCT    = 0.006

SYMBOLS = ["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX"]


# ── DB helpers ────────────────────────────────────────────────────────────────

async def fetch_session_bars(conn, symbol, day):
    rows = await conn.fetch("""
        SELECT (time AT TIME ZONE 'Asia/Kolkata') AS ts_ist,
               open, high, low, close
        FROM market_candles
        WHERE symbol = $1
          AND (time AT TIME ZONE 'Asia/Kolkata')::date = $2
          AND EXTRACT(HOUR FROM time AT TIME ZONE 'Asia/Kolkata') BETWEEN 9 AND 15
        ORDER BY time
    """, symbol, day)
    out = []
    for r in rows:
        d = dict(r)
        for k in ("open", "high", "low", "close"):
            d[k] = float(d[k])
        out.append(d)
    return out


async def fetch_decisions(conn, symbol, day):
    rows = await conn.fetch("""
        SELECT (time AT TIME ZONE 'Asia/Kolkata') AS ts_ist,
               decision, confidence, indicators_snapshot
        FROM ai_decisions
        WHERE symbol = $1
          AND (time AT TIME ZONE 'Asia/Kolkata')::date = $2
          AND decision IN ('BUY', 'SELL')
          AND confidence >= 0.70
        ORDER BY time
    """, symbol, day)
    return [dict(r) for r in rows]


# ── classify the day (re-uses logic from backtest_orb_after_break.py) ─────────

def classify_day(orb_high, orb_low, post_bars):
    th_high = orb_high * (1 + ORB_BUFFER)
    th_low  = orb_low  * (1 - ORB_BUFFER)

    first_idx, first_dir = None, None
    for i, b in enumerate(post_bars):
        if b["close"] > th_high:
            first_idx, first_dir = i, "UP"; break
        if b["close"] < th_low:
            first_idx, first_dir = i, "DOWN"; break

    if first_idx is None:
        return "NEVER_BROKE", None, None

    returned, same_again, broke_other = False, False, False
    for b in post_bars[first_idx + 1:]:
        close = b["close"]
        if orb_low <= close <= orb_high:
            returned = True
        if returned:
            if first_dir == "UP" and close < th_low:
                broke_other = True; break
            if first_dir == "DOWN" and close > th_high:
                broke_other = True; break
            if (first_dir == "UP"   and close > th_high) or \
               (first_dir == "DOWN" and close < th_low):
                same_again = True

    if not returned:           return "CLEAN_CONT",    first_dir, post_bars[first_idx]
    if broke_other:            return "REVERSAL",      first_dir, post_bars[first_idx]
    if same_again:             return "SAME_AGAIN",    first_dir, post_bars[first_idx]
    return "MEAN_REVERTED", first_dir, post_bars[first_idx]


# ── walk-forward evaluator on underlying ──────────────────────────────────────

def evaluate(side, entry_price, post_bars, start_idx):
    """Walk 1m bars from start_idx+1 onward; return WIN | LOSS | TIMEOUT."""
    if side == "BUY":
        sl  = entry_price * (1 - SL_PCT)
        tgt = entry_price * (1 + TGT_PCT)
        for b in post_bars[start_idx + 1:]:
            if b["low"]  <= sl:  return "LOSS"
            if b["high"] >= tgt: return "WIN"
        return "TIMEOUT"
    else:  # SELL
        sl  = entry_price * (1 + SL_PCT)
        tgt = entry_price * (1 - TGT_PCT)
        for b in post_bars[start_idx + 1:]:
            if b["high"] >= sl:  return "LOSS"
            if b["low"]  <= tgt: return "WIN"
        return "TIMEOUT"


def bar_index_at(post_bars, ts_ist):
    """Return the index of the first 1m bar at or after ts_ist."""
    target = ts_ist
    for i, b in enumerate(post_bars):
        if b["ts_ist"] >= target:
            return i
    return None


# ── rule semantics ────────────────────────────────────────────────────────────

def rule_legacy_taken(side, current_price, orb_high, orb_low):
    """No ORB-after-break amnesty at all (the rule in master)."""
    th_high = orb_high * (1 + ORB_BUFFER)
    th_low  = orb_low  * (1 - ORB_BUFFER)
    if side == "BUY":   return current_price > th_high
    if side == "SELL":  return current_price < th_low
    return False


def rule_current_taken(side, current_price, orb_high, orb_low, broken_yet, _break_dir):
    """The rule we just shipped: any direction allowed once broken."""
    if broken_yet:
        return True
    return rule_legacy_taken(side, current_price, orb_high, orb_low)


def rule_variant_taken(side, current_price, orb_high, orb_low, broken_yet, break_dir):
    """Variant 1: only same-direction-as-first-break gets amnesty."""
    if broken_yet:
        if (break_dir == "UP" and side == "BUY") or \
           (break_dir == "DOWN" and side == "SELL"):
            return True
    return rule_legacy_taken(side, current_price, orb_high, orb_low)


# ── main ──────────────────────────────────────────────────────────────────────

async def main():
    conn = await asyncpg.connect(DB_DSN)
    dates = await conn.fetch("""
        SELECT DISTINCT (time AT TIME ZONE 'Asia/Kolkata')::date AS d
        FROM market_candles
        WHERE symbol = ANY($1::text[])
          AND EXTRACT(HOUR FROM time AT TIME ZONE 'Asia/Kolkata') BETWEEN 9 AND 15
        ORDER BY d
    """, SYMBOLS)
    dates = [r["d"] for r in dates]

    # bucket -> rule -> outcome -> count
    stats = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    # rule -> total PnL units (WIN = +TGT_PCT, LOSS = -SL_PCT, TIMEOUT = 0)
    pnl   = defaultdict(float)
    # rule -> trades taken
    trades_total = defaultdict(int)

    for d in dates:
        for sym in SYMBOLS:
            bars = await fetch_session_bars(conn, sym, d)
            if len(bars) < 30:
                continue
            orb_bars  = [b for b in bars if dtime(9, 15) <= b["ts_ist"].time() < dtime(9, 30)]
            post_bars = [b for b in bars if dtime(9, 30) <= b["ts_ist"].time() <= dtime(15, 30)]
            if not orb_bars or not post_bars:
                continue
            orb_high = max(b["high"] for b in orb_bars)
            orb_low  = min(b["low"]  for b in orb_bars)
            bucket, first_dir, first_break_bar = classify_day(orb_high, orb_low, post_bars)
            if bucket == "NEVER_BROKE":
                continue

            decisions = await fetch_decisions(conn, sym, d)
            for dec in decisions:
                # only decisions after the first break — that's when amnesty kicks in
                if dec["ts_ist"] < first_break_bar["ts_ist"]:
                    continue
                # pull live price from indicators_snapshot (matches what the gate saw)
                ind = dec["indicators_snapshot"]
                if isinstance(ind, str):
                    ind = json.loads(ind or "{}")
                price = float(ind.get("price") or 0)
                if price <= 0:
                    continue

                side  = dec["decision"]
                bar_i = bar_index_at(post_bars, dec["ts_ist"])
                if bar_i is None or bar_i >= len(post_bars) - 1:
                    continue   # too late in the day to evaluate

                for rule_name, rule_fn in (
                    ("legacy",  lambda s, p: rule_legacy_taken(s, p, orb_high, orb_low)),
                    ("current", lambda s, p: rule_current_taken(s, p, orb_high, orb_low, True, first_dir)),
                    ("variant", lambda s, p: rule_variant_taken(s, p, orb_high, orb_low, True, first_dir)),
                ):
                    if rule_fn(side, price):
                        outcome = evaluate(side, price, post_bars, bar_i)
                        stats[bucket][rule_name][outcome] += 1
                        trades_total[rule_name] += 1
                        if outcome == "WIN":     pnl[rule_name] += TGT_PCT
                        elif outcome == "LOSS":  pnl[rule_name] -= SL_PCT

    await conn.close()

    # ── report ────────────────────────────────────────────────────────────────
    print("=" * 88)
    print(f"Variant 1 backtest — direction-aligned ORB-after-break amnesty")
    print(f"Date range: {dates[0]} → {dates[-1]}    SL={SL_PCT*100:.2f}%   TGT={TGT_PCT*100:.2f}%   "
          f"RR={TGT_PCT/SL_PCT:.1f}")
    print("=" * 88)

    rule_order = ["legacy", "current", "variant"]
    buckets = ["CLEAN_CONT", "SAME_AGAIN", "MEAN_REVERTED", "REVERSAL"]

    for bucket in buckets:
        print(f"\n── {bucket} ──")
        print(f"  {'rule':<10} {'trades':<8} {'WIN':<6} {'LOSS':<7} {'TIMEOUT':<8} {'win%':<6} {'edge%'}")
        for rule in rule_order:
            row = stats[bucket][rule]
            w, l, t = row["WIN"], row["LOSS"], row["TIMEOUT"]
            n = w + l + t
            if n == 0:
                print(f"  {rule:<10} 0")
                continue
            wp = w / n * 100
            edge = (w * TGT_PCT - l * SL_PCT) * 100  # in percentage-points
            print(f"  {rule:<10} {n:<8} {w:<6} {l:<7} {t:<8} {wp:<6.1f} {edge:+.2f}")

    print("\n── overall ──")
    print(f"  {'rule':<10} {'trades':<8} {'cumulative PnL (% underlying)'}")
    for rule in rule_order:
        n = trades_total[rule]
        print(f"  {rule:<10} {n:<8} {pnl[rule]*100:+.2f}%   "
              f"(avg per trade: {(pnl[rule]/n*100 if n else 0):+.3f}%)")


if __name__ == "__main__":
    asyncio.run(main())
