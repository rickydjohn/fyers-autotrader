#!/usr/bin/env python3
"""
ORB buffer PnL study — compare confirmation buffers on a 2-year ORB-breakout
simulation, to decide between price-relative (deployed 0.20%) and range-relative
(% of ORB width) buffers on REALISED outcomes, not just false-break filtering.

Why a fresh sim (not backtest_orb_gate.py): the live trade log only holds trades
that already passed the deployed 0.20% gate, so it can't evaluate a *looser*
buffer's newly-admitted trades (selection bias). Simulating breakouts on the raw
index has no such bias.

CAVEAT: this trades the INDEX (points), not the option+LLM system. Absolute PnL
is NOT the system's PnL. Use it for the RELATIVE ranking of buffers only.

Strategy (one trade/day, mirrors the gate's first-break-sets-direction rule):
  * ORB = 09:15-09:29 high/low ; width = high-low.
  * Enter the first confirmed breakout after 09:30: long @ orb_high+buf when a bar
    trades there, short @ orb_low-buf. (buffer-level fill; slippage ignored —
    constant across configs.)
  * Two exit variants, both reported:
      HOLD  : exit at 15:25 close.
      STOP  : exit at the broken edge if price CLOSES back inside the range
              (false-break stop), else 15:25 close.

Run:  python tests/backtests/backtest_orb_buffer_pnl.py [csv_path]
"""

import sys
from datetime import time as dtime

import numpy as np
import pandas as pd

CSV_PATH         = sys.argv[1] if len(sys.argv) > 1 else "/tmp/orb_candles_fyers.csv"
OPEN_START       = dtime(9, 15)
OPEN_END         = dtime(9, 30)
EXIT_CUTOFF      = dtime(15, 25)
SESSION_END      = dtime(15, 30)
MIN_OPENING_BARS = 10
MIN_DAY_BARS     = 200
SYMBOLS          = ["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX"]

# (label, kind, value)  kind: "price" -> value*price ; "width" -> value*ORB_width
CONFIGS = [
    ("price 0.15%", "price", 0.0015),
    ("price 0.20%*", "price", 0.0020),   # deployed
    ("price 0.25%", "price", 0.0025),
    ("width 20%",   "width", 0.20),
    ("width 25%",   "width", 0.25),
    ("width 30%",   "width", 0.30),
    ("width 35%",   "width", 0.35),
]


def simulate(post, orb_h, orb_l, buf, exit_close, use_stop):
    """Return pnl_pts (signed) or None if no breakout cleared the buffer."""
    th_h, th_l = orb_h + buf, orb_l - buf
    side = entry = None
    start = None
    for i, b in enumerate(post):
        if b["high"] >= th_h:
            side, entry, start = "L", th_h, i; break
        if b["low"] <= th_l:
            side, entry, start = "S", th_l, i; break
    if side is None:
        return None

    exit_px = exit_close
    if use_stop:
        for b in post[start:]:
            if side == "L" and b["close"] <= orb_h:
                exit_px = orb_h; break
            if side == "S" and b["close"] >= orb_l:
                exit_px = orb_l; break
    return (exit_px - entry) if side == "L" else (entry - exit_px)


def stats(pnls_pct):
    a = np.array(pnls_pct, float)
    if not len(a):
        return None
    wins, losses = a[a > 0], a[a < 0]
    pf = wins.sum() / abs(losses.sum()) if losses.sum() != 0 else float("inf")
    return {
        "n": len(a), "win%": len(wins) / len(a) * 100,
        "avg": a.mean(), "total": a.sum(), "pf": pf,
        "avg_win": wins.mean() if len(wins) else 0,
        "avg_loss": losses.mean() if len(losses) else 0,
    }


def run_symbol(df_sym):
    days = []
    for day, g in df_sym.groupby(df_sym["ts"].dt.date):
        g = g.sort_values("ts")
        op = g[(g["ts"].dt.time >= OPEN_START) & (g["ts"].dt.time < OPEN_END)]
        intr = g[(g["ts"].dt.time >= OPEN_START) & (g["ts"].dt.time <= SESSION_END)]
        if len(op) < MIN_OPENING_BARS or len(intr) < MIN_DAY_BARS:
            continue
        orb_h, orb_l = float(op["high"].max()), float(op["low"].min())
        width = orb_h - orb_l
        if width <= 0:
            continue
        mid = (orb_h + orb_l) / 2
        post = intr[intr["ts"].dt.time >= OPEN_END][["ts", "high", "low", "close"]].to_dict("records")
        ec = intr[intr["ts"].dt.time <= EXIT_CUTOFF]
        exit_close = float(ec.iloc[-1]["close"]) if len(ec) else float(intr.iloc[-1]["close"])
        days.append((orb_h, orb_l, width, mid, post, exit_close))

    results = {}
    for label, kind, val in CONFIGS:
        for variant, use_stop in (("HOLD", False), ("STOP", True)):
            pnls = []
            for orb_h, orb_l, width, mid, post, exit_close in days:
                buf = val * mid if kind == "price" else val * width
                p = simulate(post, orb_h, orb_l, buf, exit_close, use_stop)
                if p is not None:
                    pnls.append(p / mid * 100)   # % of price
            results[(label, variant)] = stats(pnls)
    return len(days), results


def main():
    df = pd.read_csv(CSV_PATH, parse_dates=["ts_ist"]).rename(columns={"ts_ist": "ts"})
    df = df[(df["ts"].dt.time >= OPEN_START) & (df["ts"].dt.time <= SESSION_END)]
    for c in ("high", "low", "close"):
        df[c] = df[c].astype(float)
    print(f"Loaded {len(df):,} intraday bars from {CSV_PATH}")

    for sym in SYMBOLS:
        d = df[df["symbol"] == sym]
        if d.empty:
            continue
        ndays, res = run_symbol(d)
        print(f"\n{'='*88}\n  {sym}   ({ndays} days)\n{'='*88}")
        for variant in ("HOLD", "STOP"):
            print(f"\n  ── exit = {variant} "
                  f"{'(hold to 15:25)' if variant=='HOLD' else '(stop on range re-entry, else 15:25)'} ──")
            print(f"    {'buffer':<13}{'trades':>7}{'win%':>7}{'avg%':>8}{'total%':>9}{'PF':>6}"
                  f"{'avgWin':>8}{'avgLoss':>9}")
            for label, _, _ in CONFIGS:
                s = res[(label, variant)]
                if not s:
                    continue
                print(f"    {label:<13}{s['n']:>7}{s['win%']:>6.0f}%{s['avg']:>+8.3f}"
                      f"{s['total']:>+9.1f}{s['pf']:>6.2f}{s['avg_win']:>+8.3f}{s['avg_loss']:>+9.3f}")


if __name__ == "__main__":
    main()
