#!/usr/bin/env python3
"""
ORB gate sweep WITH full chain — applies every other entry gate from
simulation-engine/main.py on top of the candidate ORB rule.

Gates replicated (in order):
  1. CONF FLOOR (0.70)
  2. Pre-09:30 IST
  3. Session cutoff (>= 15:15 IST)
  4. ORB BREAKOUT (the rule being swept) — with same-day-broken relaxation
  5. CPR no-trade bracket  [min(TC,BC)*0.998, max(TC,BC)*1.002]
  6. CONSOLIDATION GATE  (consolidation_pct < 0.40 AND no-/wrong-direction breakout)
  7. PA proximity (PA_PROXIMITY = 0.0025), DayHigh/DayLow excluded

Skipped (too complex to replicate offline):
  - Pre-entry exit simulation (exit_rules.py)

Run inside trading-data container:
    docker cp tests/backtests/backtest_orb_chain_sweep.py trading-data:/tmp/
    docker exec trading-data python /tmp/backtest_orb_chain_sweep.py
"""

import asyncio
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, time as dtime

import asyncpg
import pytz

DB_DSN     = "postgresql://trading:trading@timescaledb:5432/trading"
IST        = pytz.timezone("Asia/Kolkata")

WINDOW_DAYS      = int(os.environ.get("WINDOW_DAYS", "30"))
LOOKFWD_MIN      = int(os.environ.get("LOOKFWD_MIN", "30"))
FAV_THRESHOLD    = 0.0020
ADV_THRESHOLD    = 0.0020
CONF_FLOOR       = 0.70
CPR_BUFFER       = 0.002
PA_PROXIMITY     = 0.0025

OLD_PCT_OF_PRICE = 0.0020
SWEEP_VALUES     = [0.10, 0.15, 0.20, 0.25, 0.30, 0.40]


# ── Symbol helpers ────────────────────────────────────────────────────────────

def _under_symbol(sym: str) -> str:
    if "BANKNIFTY" in (sym or ""):
        return "NSE:NIFTYBANK-INDEX"
    return "NSE:NIFTY50-INDEX"


# ── ORB rules ─────────────────────────────────────────────────────────────────

def _orb_threshold_old(oh: float, ol: float):
    return oh * (1 + OLD_PCT_OF_PRICE), ol * (1 - OLD_PCT_OF_PRICE)


def _orb_threshold_new(oh: float, ol: float, pct: float):
    buf = (oh - ol) * pct
    return oh + buf, ol - buf


# ── Gate chain ────────────────────────────────────────────────────────────────

def _gates_pre_orb(side: str, conf: float, when_ist) -> str | None:
    """Returns blocking gate name or None if all pre-ORB gates pass."""
    if conf < CONF_FLOOR:
        return "CONF"
    if when_ist.time() < dtime(9, 30):
        return "BEFORE_ORB"
    if when_ist.time() >= dtime(15, 15):
        return "SESSION_CUTOFF"
    return None


def _gates_post_orb(side: str, price: float, snap: dict) -> str | None:
    """Gates after ORB: CPR, consolidation, PA proximity."""
    # CPR
    tc = float(snap.get("cpr_tc") or 0)
    bc = float(snap.get("cpr_bc") or 0)
    if tc > 0 and bc > 0:
        upper = max(tc, bc) * (1 + CPR_BUFFER)
        lower = min(tc, bc) * (1 - CPR_BUFFER)
        if lower <= price <= upper:
            return "CPR"

    # Consolidation
    rb   = snap.get("range_breakout") or ""
    cpct = float(snap.get("consolidation_pct") or 1.0)
    if cpct < 0.40:
        if rb == "NONE":
            return "CONSOLIDATION"
        if side == "BUY"  and rb == "BREAKOUT_LOW":
            return "CONSOLIDATION"
        if side == "SELL" and rb == "BREAKOUT_HIGH":
            return "CONSOLIDATION"

    # PA proximity
    if side == "BUY":
        nr  = float(snap.get("nearest_resistance") or 0)
        lbl = snap.get("nearest_resistance_label") or ""
        if nr > 0 and lbl != "DayHigh" and nr * (1 - PA_PROXIMITY) <= price <= nr:
            return "PA"
    else:
        ns  = float(snap.get("nearest_support") or 0)
        lbl = snap.get("nearest_support_label") or ""
        if ns > 0 and lbl != "DayLow" and ns <= price <= ns * (1 + PA_PROXIMITY):
            return "PA"
    return None


def _orb_blocks(side: str, price: float, oh: float, ol: float,
                pre_break: bool, mode: str, pct: float | None) -> bool:
    """True if ORB gate would block. pre_break=True means relaxation hasn't fired."""
    if not oh or not ol:
        return False
    if not pre_break:
        return False
    if mode == "OLD":
        th_hi, th_lo = _orb_threshold_old(oh, ol)
    else:
        th_hi, th_lo = _orb_threshold_new(oh, ol, pct)
    if side == "BUY"  and price <= th_hi:
        return True
    if side == "SELL" and price >= th_lo:
        return True
    return False


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _fetch_day_extremes(conn, symbol: str, day) -> list:
    """Return (ts_ist, high, low) ordered for a (symbol, day)."""
    rows = await conn.fetch("""
        SELECT (time AT TIME ZONE 'Asia/Kolkata') AS ts_ist, high, low
        FROM market_candles
        WHERE symbol = $1
          AND (time AT TIME ZONE 'Asia/Kolkata')::date = $2
          AND EXTRACT(HOUR FROM time AT TIME ZONE 'Asia/Kolkata') >= 9
          AND (EXTRACT(HOUR FROM time AT TIME ZONE 'Asia/Kolkata') > 9
               OR EXTRACT(MINUTE FROM time AT TIME ZONE 'Asia/Kolkata') >= 30)
        ORDER BY time
    """, symbol, day)
    return [(r["ts_ist"], float(r["high"]), float(r["low"])) for r in rows]


def _orb_break_time(extremes: list, th_high: float, th_low: float):
    """Earliest IST time when intraday session high/low broke ORB thresholds."""
    sess_hi = -1e18
    sess_lo = 1e18
    for ts, h, l in extremes:
        sess_hi = max(sess_hi, h)
        sess_lo = min(sess_lo, l)
        if sess_hi > th_high or sess_lo < th_low:
            return ts
    return None


async def _fetch_forward(conn, symbol: str, start_ist, minutes: int):
    end_ist = start_ist + timedelta(minutes=minutes)
    rows = await conn.fetch("""
        SELECT (time AT TIME ZONE 'Asia/Kolkata') AS ts_ist, high, low, close
        FROM market_candles
        WHERE symbol = $1 AND time > $2 AND time <= $3
        ORDER BY time
    """, symbol, start_ist, end_ist)
    return [(r["ts_ist"], float(r["high"]), float(r["low"]), float(r["close"])) for r in rows]


def _classify(side: str, entry: float, fwd) -> str:
    max_fav = max_adv = 0.0
    for _ts, h, l, _c in fwd:
        if side == "BUY":
            fav = (h - entry) / entry
            adv = (entry - l) / entry
        else:
            fav = (entry - l) / entry
            adv = (h - entry) / entry
        if fav > max_fav: max_fav = fav
        if adv > max_adv: max_adv = adv
    if max_fav >= FAV_THRESHOLD and max_fav > max_adv:
        return "WOULD-WIN"
    if max_adv >= ADV_THRESHOLD:
        return "WOULD-LOSE"
    return "INCONCLUSIVE"


# ── Main sweep ────────────────────────────────────────────────────────────────

async def evaluate(conn, decisions, pct: float, extremes_cache: dict):
    """For a single NEW_PCT value, return summary stats and per-day breakdown."""
    # Cache ORB-break-time per (symbol, day) for OLD and NEW
    orb_break_old: dict = {}
    orb_break_new: dict = {}

    def _get_break_old(key, oh, ol):
        if key in orb_break_old:
            return orb_break_old[key]
        ex = extremes_cache.get(key, [])
        th_hi, th_lo = _orb_threshold_old(oh, ol)
        t = _orb_break_time(ex, th_hi, th_lo)
        orb_break_old[key] = t
        return t

    def _get_break_new(key, oh, ol, pct_):
        ck = (key, pct_)
        if ck in orb_break_new:
            return orb_break_new[ck]
        ex = extremes_cache.get(key, [])
        th_hi, th_lo = _orb_threshold_new(oh, ol, pct_)
        t = _orb_break_time(ex, th_hi, th_lo)
        orb_break_new[ck] = t
        return t

    unlocked = []
    counts_old = Counter()      # gate that blocked under OLD chain
    counts_new = Counter()      # gate that blocked under NEW chain
    pass_old = pass_new = 0

    for row in decisions:
        snap = row["snap"]
        side = row["decision"]
        conf = row["confidence"]
        when_ist = row["when_ist"]
        sym  = row["symbol"]
        und  = _under_symbol(sym)
        day  = when_ist.date()

        # 1-3: pre-ORB
        pre = _gates_pre_orb(side, conf, when_ist)
        if pre:
            counts_old[pre] += 1
            counts_new[pre] += 1
            continue

        oh = float(snap.get("orb_high") or 0)
        ol = float(snap.get("orb_low")  or 0)
        price = float(snap.get("price") or 0)

        key = (und, day)
        break_old = _get_break_old(key, oh, ol)
        break_new = _get_break_new(key, oh, ol, pct)

        # Postgres "time AT TIME ZONE 'Asia/Kolkata'" returns naive timestamps;
        # strip tz from when_ist for the comparison.
        when_naive = when_ist.replace(tzinfo=None)
        pre_break_old = (break_old is None) or (when_naive < break_old)
        pre_break_new = (break_new is None) or (when_naive < break_new)

        # 4: ORB
        orb_old = _orb_blocks(side, price, oh, ol, pre_break_old, "OLD", None)
        orb_new = _orb_blocks(side, price, oh, ol, pre_break_new, "NEW", pct)

        # 5-7: post-ORB
        post = _gates_post_orb(side, price, snap)

        # Final verdict under each chain
        old_block_reason = None
        if orb_old:                old_block_reason = "ORB"
        elif post:                 old_block_reason = post

        new_block_reason = None
        if orb_new:                new_block_reason = "ORB"
        elif post:                 new_block_reason = post

        if old_block_reason is None:  pass_old += 1
        else:                          counts_old[old_block_reason] += 1
        if new_block_reason is None:  pass_new += 1
        else:                          counts_new[new_block_reason] += 1

        if old_block_reason is not None and new_block_reason is None:
            unlocked.append(row)

    return {
        "pass_old": pass_old,
        "pass_new": pass_new,
        "block_old": counts_old,
        "block_new": counts_new,
        "unlocked": unlocked,
    }


async def main():
    print("=" * 78)
    print(f"  ORB SWEEP w/ FULL CHAIN  (last {WINDOW_DAYS} days)")
    print("=" * 78)

    conn = await asyncpg.connect(DB_DSN)
    rows = await conn.fetch("""
        SELECT decision_id, time, symbol, decision, confidence, acted_upon,
               indicators_snapshot
        FROM ai_decisions
        WHERE symbol IN ('NSE:NIFTY50-INDEX','NSE:NIFTYBANK-INDEX')
          AND decision IN ('BUY','SELL')
          AND time >= NOW() - ($1 || ' days')::interval
        ORDER BY time
    """, str(WINDOW_DAYS))
    print(f"  BUY/SELL decisions: {len(rows)}")

    # Pre-parse decisions
    decisions = []
    days_needed = set()
    for r in rows:
        snap = r["indicators_snapshot"]
        if isinstance(snap, str):
            snap = json.loads(snap)
        if not snap or not snap.get("orb_high") or not snap.get("price"):
            continue
        when_ist = r["time"].astimezone(IST)
        decisions.append({
            "decision_id": r["decision_id"],
            "when_ist":    when_ist,
            "symbol":      r["symbol"],
            "decision":    r["decision"],
            "confidence":  float(r["confidence"]),
            "acted_upon":  r["acted_upon"],
            "snap":        snap,
        })
        days_needed.add((_under_symbol(r["symbol"]), when_ist.date()))

    print(f"  Usable (with ORB+price): {len(decisions)}")

    # Cache intraday extremes per (symbol, day)
    print(f"  Caching intraday extremes for {len(days_needed)} (symbol,day) pairs...", flush=True)
    extremes_cache: dict = {}
    for und, day in days_needed:
        extremes_cache[(und, day)] = await _fetch_day_extremes(conn, und, day)
    print(f"  Cache ready.")

    # ── Sweep ─────────────────────────────────────────────────────────────────
    print(f"\n  {'NEW_PCT':<10} {'OLD pass':<10} {'NEW pass':<10} {'unlocked':<10} "
          f"{'WIN':>5} {'LOSE':>5} {'INC':>5}  win-rate  est-PnL")
    print(f"  {'-'*78}")

    best_results = {}
    for pct in SWEEP_VALUES:
        res = await evaluate(conn, decisions, pct, extremes_cache)

        # Forward-classify unlocked decisions
        verdicts = Counter()
        per_day = defaultdict(Counter)
        for row in res["unlocked"]:
            und = _under_symbol(row["symbol"])
            entry = float(row["snap"]["price"])
            fwd = await _fetch_forward(conn, und, row["when_ist"], LOOKFWD_MIN)
            if not fwd:
                verdicts["NO-FWD"] += 1
                continue
            v = _classify(row["decision"], entry, fwd)
            verdicts[v] += 1
            per_day[row["when_ist"].date()][v] += 1

        wins  = verdicts.get("WOULD-WIN", 0)
        loses = verdicts.get("WOULD-LOSE", 0)
        inc   = verdicts.get("INCONCLUSIVE", 0)
        decisive = wins + loses
        wr = (wins / decisive * 100) if decisive else 0
        est_pnl = wins * 4500 + loses * (-3500)

        print(f"  {pct*100:>6.0f}% of  {res['pass_old']:>8} {res['pass_new']:>9} "
              f"{len(res['unlocked']):>10}  {wins:>5} {loses:>5} {inc:>5}  "
              f"{wr:>6.1f}%  ₹{est_pnl:>+8,.0f}")

        best_results[pct] = {
            "res": res,
            "verdicts": verdicts,
            "per_day": per_day,
            "wr": wr,
            "est_pnl": est_pnl,
        }

    # ── Detail for the 20% case (current proposal) ───────────────────────────
    print("\n" + "=" * 78)
    print("  DETAIL — NEW_PCT = 20%   (current proposal)")
    print("=" * 78)
    r20 = best_results[0.20]
    res = r20["res"]
    print(f"\n  OLD chain  passes: {res['pass_old']:>5}    blocks: {dict(res['block_old'])}")
    print(f"  NEW chain  passes: {res['pass_new']:>5}    blocks: {dict(res['block_new'])}")
    print(f"\n  Newly admitted by NEW chain (vs OLD chain): {len(res['unlocked'])}")
    print(f"  Forward verdicts: {dict(r20['verdicts'])}")
    print(f"  Per-day:")
    print(f"    {'date':<12} {'WIN':>5} {'LOSE':>5} {'INC':>5}")
    for d in sorted(r20["per_day"]):
        c = r20["per_day"][d]
        print(f"    {d!s:<12} {c.get('WOULD-WIN',0):>5} "
              f"{c.get('WOULD-LOSE',0):>5} {c.get('INCONCLUSIVE',0):>5}")

    # ── Cross-check: how many of the OLD-chain passes actually became trades?
    acted = sum(1 for d in decisions if d["acted_upon"])
    print(f"\n  Cross-check: {acted} of {len(decisions)} usable decisions were acted_upon")
    print(f"               OLD chain replay says {res['pass_old']} should have passed")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
