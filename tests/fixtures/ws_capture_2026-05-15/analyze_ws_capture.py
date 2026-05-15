#!/usr/bin/env python3
"""Offline analysis of the 2026-05-15 WS capture pack.

What we want to know:
  1. Does a naive forming-bar synthesizer (open=first tick of minute, H=max,
     L=min, C=last) produce OHLC values that match Fyers' authoritative 1m
     bars from the DB?
  2. What's the inter-arrival distribution and where are the silent stretches?
  3. How does a 500ms throttle affect the Redis-visible price stream — do
     we drop meaningful price movement?
  4. How wide is a typical 1m bar for these symbols (informs UI scale)?
"""
from __future__ import annotations
import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

FIXTURE_DIR = Path("/Users/ricky/Projects/src/github.com/rickydjohn/fyers-autotrader/tests/fixtures/ws_capture_2026-05-15")
IST = timezone(timedelta(hours=5, minutes=30))


def parse_recv_ts(s: str) -> datetime:
    """_recv_ts is local time string with ms precision (no tz info)."""
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=IST)


def load_ticks() -> list[dict]:
    """Return only 'if' tick messages from the capture."""
    ticks = []
    with (FIXTURE_DIR / "tick_capture.jsonl").open() as f:
        for line in f:
            m = json.loads(line)
            if m.get("type") == "if" and m.get("symbol"):
                m["_dt"] = parse_recv_ts(m["_recv_ts"])
                ticks.append(m)
    return ticks


def load_db_bars() -> dict[tuple[str, str], dict]:
    """Return {(symbol, 'HH:MM'): bar} for today's DB bars."""
    rows = json.loads((FIXTURE_DIR / "db_market_candles_today.json").read_text())
    out = {}
    for r in rows:
        # r["t"] looks like "2026-05-15 13:48:00"
        hhmm = r["t"][11:16]   # "HH:MM"
        out[(r["symbol"], hhmm)] = r
    return out


def synthesize_forming_bars(ticks: list[dict]) -> dict[tuple[str, str], dict]:
    """Walk ticks in order, accumulate per-minute OHLC per symbol.
    Returns {(symbol, 'HH:MM'): {open, high, low, close, n, first_ts, last_ts}}.
    """
    bars: dict[tuple[str, str], dict] = {}
    for t in ticks:
        sym = t["symbol"]
        ltp = float(t["ltp"])
        key = (sym, t["_dt"].strftime("%H:%M"))
        b = bars.get(key)
        if b is None:
            bars[key] = {
                "open": ltp, "high": ltp, "low": ltp, "close": ltp,
                "n": 1, "first_ts": t["_recv_ts"], "last_ts": t["_recv_ts"],
            }
        else:
            b["high"] = max(b["high"], ltp)
            b["low"]  = min(b["low"],  ltp)
            b["close"] = ltp
            b["n"]    += 1
            b["last_ts"] = t["_recv_ts"]
    return bars


def fmt_diff(ws_v: float, db_v: float) -> str:
    diff = ws_v - db_v
    if abs(diff) < 0.005:
        return "match"
    return f"{diff:+.2f}"


def main() -> None:
    ticks = load_ticks()
    db_bars = load_db_bars()
    ws_bars = synthesize_forming_bars(ticks)

    print(f"Loaded {len(ticks)} ticks, {len(ws_bars)} synthesized bars, {len(db_bars)} DB bars\n")

    # ── 1. Forming-bar vs Fyers DB bar comparison ────────────────────────────
    print("=" * 90)
    print("FORMING-BAR vs FYERS DB 1m BAR COMPARISON")
    print("=" * 90)
    # Print only minutes where we have BOTH a WS synthesis AND a DB bar (excludes the
    # partial first/last minute of our 5-min capture and any future minutes the DB
    # hasn't pulled yet).
    common = sorted(set(ws_bars.keys()) & set(db_bars.keys()))
    print(f"  {len(common)} minute-bars present in BOTH WS-synth and DB\n")
    print(f"  {'symbol':22} {'HH:MM':6}  {'src':4} {'open':>10} {'high':>10} {'low':>10} {'close':>10}  diffs")
    print("  " + "-" * 100)
    for (sym, hhmm) in common:
        ws = ws_bars[(sym, hhmm)]
        db = db_bars[(sym, hhmm)]
        diffs = (
            f"O={fmt_diff(ws['open'], db['open'])}, "
            f"H={fmt_diff(ws['high'], db['high'])}, "
            f"L={fmt_diff(ws['low'], db['low'])}, "
            f"C={fmt_diff(ws['close'], db['close'])}"
        )
        print(f"  {sym:22} {hhmm}  WS  {ws['open']:>10.2f} {ws['high']:>10.2f} {ws['low']:>10.2f} {ws['close']:>10.2f}")
        print(f"  {sym:22} {hhmm}  DB  {db['open']:>10.2f} {db['high']:>10.2f} {db['low']:>10.2f} {db['close']:>10.2f}  ({diffs})")
        print(f"  {sym:22} {'':6}      n_ticks={ws['n']}  first_tick={ws['first_ts'][11:23]}  last_tick={ws['last_ts'][11:23]}")
        print()

    # ── 2. Inter-arrival distribution detail ─────────────────────────────────
    print("=" * 90)
    print("INTER-ARRIVAL DISTRIBUTION (silent stretches, p99, max)")
    print("=" * 90)
    by_sym: dict[str, list[float]] = defaultdict(list)
    last_t: dict[str, datetime] = {}
    for t in ticks:
        sym = t["symbol"]
        if sym in last_t:
            delta_ms = (t["_dt"] - last_t[sym]).total_seconds() * 1000
            by_sym[sym].append(delta_ms)
        last_t[sym] = t["_dt"]
    print(f"  {'symbol':22} {'count':>6} {'min':>6} {'p50':>6} {'p95':>6} {'p99':>6} {'max':>6}  {'>1s':>6} {'>2s':>6}")
    for sym in sorted(by_sym):
        gs = sorted(by_sym[sym])
        n = len(gs)
        over_1s = sum(1 for g in gs if g > 1000)
        over_2s = sum(1 for g in gs if g > 2000)
        print(f"  {sym:22} {n:>6} {gs[0]:>6.0f} {gs[n//2]:>6.0f} {gs[int(n*.95)]:>6.0f} {gs[int(n*.99)]:>6.0f} {gs[-1]:>6.0f}  {over_1s:>6} {over_2s:>6}")
    print()

    # ── 3. 500ms throttle simulation ─────────────────────────────────────────
    print("=" * 90)
    print("500ms-THROTTLE SIMULATION (what Redis actually sees)")
    print("=" * 90)
    print("  For each symbol: count of ticks vs count of writes under a 500ms-per-symbol throttle.")
    print("  'max suppressed Δprice' = the largest price move between a kept write and the next.\n")
    kept = defaultdict(int)
    last_kept_ms: dict[str, float] = {}
    last_kept_price: dict[str, float] = {}
    max_suppressed_delta: dict[str, float] = defaultdict(float)
    for t in ticks:
        sym = t["symbol"]
        ts_ms = t["_dt"].timestamp() * 1000
        if sym not in last_kept_ms or (ts_ms - last_kept_ms[sym]) >= 500:
            kept[sym] += 1
            last_kept_ms[sym] = ts_ms
            last_kept_price[sym] = float(t["ltp"])
        else:
            # suppressed — how far has price moved since last kept write?
            delta = abs(float(t["ltp"]) - last_kept_price[sym])
            if delta > max_suppressed_delta[sym]:
                max_suppressed_delta[sym] = delta
    for sym in sorted(by_sym):
        total = len([t for t in ticks if t["symbol"] == sym])
        print(f"  {sym:22} ticks={total} writes={kept[sym]} suppressed={total-kept[sym]}  "
              f"max Δprice between writes: {max_suppressed_delta[sym]:.2f}")
    print()

    # ── 4. 1m bar range stats ────────────────────────────────────────────────
    print("=" * 90)
    print("1m BAR RANGE (high-low) across our synthesized bars")
    print("=" * 90)
    for sym in sorted({s for s, _ in ws_bars}):
        ranges = []
        for (s, _), b in ws_bars.items():
            if s != sym:
                continue
            ranges.append(b["high"] - b["low"])
        ranges.sort()
        n = len(ranges)
        if n == 0:
            continue
        print(f"  {sym:22} bars={n}  min={ranges[0]:.2f}  p50={ranges[n//2]:.2f}  max={ranges[-1]:.2f}  avg={sum(ranges)/n:.2f}")


if __name__ == "__main__":
    main()
