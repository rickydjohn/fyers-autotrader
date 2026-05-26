#!/usr/bin/env python3
"""
ORB gate backtest — compare OLD buffer (0.20% of price) vs NEW buffer
(20% of ORB range).

Two parts:

  A1  Trade-replay: every executed trade in `trades:all`, classify under both
      gates. (Note: trades after ORB-gate deployment all passed OLD by
      construction; useful mainly to inspect pre-deployment trades and to
      confirm NEW is at least as permissive.)

  A2  Decision-replay: every BUY/SELL ai_decisions row over the last N days.
      Find decisions that OLD ORB blocks but NEW ORB admits, then evaluate
      30-min forward underlying movement and classify each as:
        WOULD-WIN     favorable move ≥0.20% (≈+10% ATM option)
        WOULD-LOSE    adverse  move ≥0.20%
        INCONCLUSIVE  neither
      Aggregate: how many extra wins/losses would the new rule have unlocked?

Run inside trading-data container:
    docker cp tests/backtests/backtest_orb_gate_dynamic.py trading-data:/tmp/
    docker exec trading-data python /tmp/backtest_orb_gate_dynamic.py
"""

import asyncio
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import asyncpg
import pytz
import redis.asyncio as aioredis

DB_DSN      = "postgresql://trading:trading@timescaledb:5432/trading"
REDIS_URL   = "redis://trading-redis:6379"
IST         = pytz.timezone("Asia/Kolkata")

OLD_PCT_OF_PRICE = float(os.environ.get("OLD_PCT", "0.0020"))   # 0.20% of price
NEW_PCT_OF_RANGE = float(os.environ.get("NEW_PCT", "0.20"))     # 20% of ORB range
LOOKFWD_MIN      = int(os.environ.get("LOOKFWD_MIN", "30"))
FAV_THRESHOLD    = 0.0020
ADV_THRESHOLD    = 0.0020
WINDOW_DAYS      = int(os.environ.get("WINDOW_DAYS", "30"))


# ── Gate helpers ──────────────────────────────────────────────────────────────

def _old_blocked(side: str, price: float, oh: float, ol: float) -> bool:
    if not oh or not ol:
        return False
    if side == "BUY":
        return price <= oh * (1 + OLD_PCT_OF_PRICE)
    return price >= ol * (1 - OLD_PCT_OF_PRICE)


def _new_blocked(side: str, price: float, oh: float, ol: float) -> bool:
    if not oh or not ol:
        return False
    buf = (oh - ol) * NEW_PCT_OF_RANGE
    if side == "BUY":
        return price <= oh + buf
    return price >= ol - buf


def _under_symbol(sym: str) -> str:
    if "BANKNIFTY" in (sym or ""):
        return "NSE:NIFTYBANK-INDEX"
    return "NSE:NIFTY50-INDEX"


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _fetch_orb(conn, symbol: str, day) -> tuple[float, float]:
    rows = await conn.fetch("""
        SELECT high, low FROM market_candles
        WHERE symbol = $1
          AND (time AT TIME ZONE 'Asia/Kolkata')::date = $2
          AND EXTRACT(HOUR   FROM time AT TIME ZONE 'Asia/Kolkata') = 9
          AND EXTRACT(MINUTE FROM time AT TIME ZONE 'Asia/Kolkata') >= 15
          AND EXTRACT(MINUTE FROM time AT TIME ZONE 'Asia/Kolkata') < 30
    """, symbol, day)
    if not rows:
        return 0.0, 0.0
    return (
        float(max(r["high"] for r in rows)),
        float(min(r["low"]  for r in rows)),
    )


async def _price_at(conn, symbol: str, when_ist) -> float | None:
    row = await conn.fetchrow("""
        SELECT close FROM market_candles
        WHERE symbol = $1
          AND (time AT TIME ZONE 'Asia/Kolkata')::date = $2
        ORDER BY ABS(EXTRACT(EPOCH FROM (time - $3)))
        LIMIT 1
    """, symbol, when_ist.date(), when_ist)
    return float(row["close"]) if row else None


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


# ── Part A1 — trade replay ────────────────────────────────────────────────────

async def trade_replay(conn):
    print("=" * 78)
    print("  A1 — TRADE REPLAY  (executed trades, both gates compared)")
    print("=" * 78)
    print(f"  OLD buffer = {OLD_PCT_OF_PRICE*100:.2f}% of price")
    print(f"  NEW buffer = {NEW_PCT_OF_RANGE*100:.0f}% of (ORB-high − ORB-low)")

    r = aioredis.from_url(REDIS_URL)
    raw = await r.hgetall("trades:all")
    await r.aclose()
    trades = [json.loads(v) for v in raw.values()]
    trades = [t for t in trades if t.get("status") in ("CLOSED", "STOPPED") and t.get("entry_time")]
    print(f"\n  Executed trades fetched: {len(trades)}")

    buckets = defaultdict(list)        # key: (old, new) tuple of booleans
    for t in trades:
        entry_dt = datetime.fromisoformat(t["entry_time"])
        if entry_dt.tzinfo is None:
            entry_dt = IST.localize(entry_dt)
        und = _under_symbol(t.get("symbol") or "")
        side = t.get("side") or "BUY"
        day = entry_dt.astimezone(IST).date()

        oh, ol = await _fetch_orb(conn, und, day)
        price = await _price_at(conn, und, entry_dt)
        if price is None or not oh:
            buckets[("no_data", "no_data")].append(t)
            continue

        b_old = _old_blocked(side, price, oh, ol)
        b_new = _new_blocked(side, price, oh, ol)
        buckets[(b_old, b_new)].append(t)

    def _agg(rows):
        if not rows: return ""
        pnls = [r.get("pnl") or 0 for r in rows]
        wins = sum(1 for p in pnls if p > 0)
        wr = wins / len(rows) * 100
        return f"n={len(rows)} wr={wr:.1f}% pnl=₹{sum(pnls):+,.0f}"

    print("\n  Matrix (rows = OLD, cols = NEW):")
    print(f"    {'':<14} {'NEW pass':<32} {'NEW block':<32}")
    for old_v in (False, True):
        old_lab = "OLD pass" if not old_v else "OLD block"
        a = buckets[(old_v, False)]
        b = buckets[(old_v, True)]
        print(f"    {old_lab:<14} {_agg(a):<32} {_agg(b):<32}")

    diff = buckets[(True, False)]
    if diff:
        print(f"\n  Trades OLD-block-but-NEW-pass (this is the 'unlocked' set):")
        for t in diff[:20]:
            print(f"    {t['entry_time'][:19]} {t['side']:<5} "
                  f"{t['symbol'].replace('NSE:','')[:24]:<24} "
                  f"pnl=₹{t.get('pnl') or 0:+,.0f} ({(t.get('pnl_pct') or 0):+.1f}%)  "
                  f"exit={t.get('exit_reason')}")


# ── Part A2 — decision replay ─────────────────────────────────────────────────

async def decision_replay(conn):
    print("\n" + "=" * 78)
    print(f"  A2 — DECISION REPLAY  (last {WINDOW_DAYS} days, BUY/SELL only)")
    print("=" * 78)

    rows = await conn.fetch("""
        SELECT decision_id, time, symbol, decision, confidence, acted_upon,
               indicators_snapshot
        FROM ai_decisions
        WHERE symbol IN ('NSE:NIFTY50-INDEX','NSE:NIFTYBANK-INDEX')
          AND decision IN ('BUY','SELL')
          AND time >= NOW() - ($1 || ' days')::interval
        ORDER BY time
    """, str(WINDOW_DAYS))
    print(f"\n  Decisions fetched: {len(rows)}")

    cats = Counter()
    unlocked = []          # decisions blocked by OLD, admitted by NEW
    for row in rows:
        snap = row["indicators_snapshot"] or {}
        if isinstance(snap, str):
            snap = json.loads(snap)
        oh = float(snap.get("orb_high") or 0)
        ol = float(snap.get("orb_low")  or 0)
        price = snap.get("price")
        if price is None or not oh:
            cats["no_data"] += 1
            continue
        price = float(price)
        side = row["decision"]
        b_old = _old_blocked(side, price, oh, ol)
        b_new = _new_blocked(side, price, oh, ol)
        key = ("old_block" if b_old else "old_pass",
               "new_block" if b_new else "new_pass")
        cats[key] += 1
        if b_old and not b_new:
            unlocked.append((row, snap))

    print("  ORB gate decision matrix:")
    for k in [("old_pass","new_pass"),("old_pass","new_block"),
              ("old_block","new_pass"),("old_block","new_block")]:
        print(f"    {k[0]:<10} → {k[1]:<10} : {cats[k]:>5}")
    if cats.get("no_data"):
        print(f"    (no_data / pre-ORB rows skipped: {cats['no_data']})")

    print(f"\n  Decisions newly admitted by NEW rule: {len(unlocked)}")
    if not unlocked:
        return

    # Forward outcome for each unlocked decision
    verdicts = Counter()
    by_day   = defaultdict(lambda: Counter())
    samples  = []

    for row, snap in unlocked:
        when_ist = row["time"].astimezone(IST)
        und = _under_symbol(row["symbol"])
        entry = float(snap["price"])
        fwd = await _fetch_forward(conn, und, when_ist, LOOKFWD_MIN)
        if not fwd:
            verdicts["NO-FORWARD-DATA"] += 1
            continue
        v = _classify(row["decision"], entry, fwd)
        verdicts[v] += 1
        by_day[when_ist.date()][v] += 1
        if len(samples) < 30:
            samples.append((when_ist, row["symbol"], row["decision"],
                            float(row["confidence"]), entry, snap.get("orb_high"),
                            snap.get("orb_low"), v))

    print(f"\n  Forward classification ({LOOKFWD_MIN}-min underlying move):")
    n = sum(v for k, v in verdicts.items() if k != "NO-FORWARD-DATA")
    for v in ("WOULD-WIN", "WOULD-LOSE", "INCONCLUSIVE"):
        c = verdicts.get(v, 0)
        pct = c / n * 100 if n else 0
        print(f"    {v:<14} {c:>4}  ({pct:.1f}%)")
    if verdicts.get("NO-FORWARD-DATA"):
        print(f"    NO-FORWARD-DATA {verdicts['NO-FORWARD-DATA']:>4}")

    wins = verdicts.get("WOULD-WIN", 0)
    losses = verdicts.get("WOULD-LOSE", 0)
    decisive = wins + losses
    print(f"\n  Among decisive outcomes: {wins} wins / {decisive} = "
          f"{(wins/decisive*100 if decisive else 0):.1f}% win-rate")
    # Estimated PnL with 75 lots * 100 px avg * (+/-10%) per side
    # but more honest: just show win/loss tally and let user judge
    est_per_win  = 4500
    est_per_loss = -3500
    est_pnl = wins * est_per_win + losses * est_per_loss
    print(f"  Estimated net PnL (₹4500/win, −₹3500/loss): ₹{est_pnl:+,.0f}")

    print("\n  Samples (first 30):")
    print(f"    {'when (IST)':<20} {'sym':<10} {'side':<5} {'conf':<5} "
          f"{'price':>9}  {'ORB-H/L':<18} verdict")
    for s in samples:
        when, sym, side, conf, entry, oh, ol, v = s
        print(f"    {when.strftime('%Y-%m-%d %H:%M:%S'):<20} "
              f"{sym.replace('NSE:','').replace('-INDEX',''):<10} "
              f"{side:<5} {conf:<5.2f} {entry:>9.2f}  "
              f"{f'{oh:.1f}/{ol:.1f}':<18} {v}")

    print("\n  Daily breakdown:")
    print(f"    {'date':<12} {'WIN':>5} {'LOSE':>5} {'INC':>5}")
    for d in sorted(by_day):
        c = by_day[d]
        print(f"    {d!s:<12} {c.get('WOULD-WIN',0):>5} "
              f"{c.get('WOULD-LOSE',0):>5} {c.get('INCONCLUSIVE',0):>5}")


async def main():
    conn = await asyncpg.connect(DB_DSN)
    try:
        await trade_replay(conn)
        await decision_replay(conn)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
