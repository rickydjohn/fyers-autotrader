#!/usr/bin/env python3
"""
Consolidation-gate evaluation.

Question (Ricky): is the consolidation gate needed? if so, do we need a range,
and what threshold is sensible?

Unbiased data: ai_decisions stores EVERY BUY/SELL signal (acted on or not) with
the consolidation_pct + range_breakout the gate saw, and the underlying price.
We score each signal by the forward directional move of the underlying (15/30/60
min, in the signal's direction) — so we test the gate's PREMISE (entering during
tight chop underperforms) with no survivorship bias.

CAVEAT: forward index move is a DIRECTIONAL proxy, not option PnL. Signals are
serially correlated (same symbol, adjacent minutes) so effective N < raw N.

Gate (as deployed, main.py): block when consolidation_pct < 0.20 AND
  (range_breakout == NONE  or  BUY vs BREAKOUT_LOW  or  SELL vs BREAKOUT_HIGH).

Inputs:
  /tmp/decisions.csv          (t,symbol,decision,confidence,consol,brk,price,acted_upon)
  /tmp/orb_candles_fyers.csv  (ts_ist,symbol,open,high,low,close)   forward prices

Run: python tests/backtests/backtest_consolidation_gate.py
"""

import numpy as np
import pandas as pd
from datetime import time as dtime, timedelta

DEC_CSV  = "/tmp/decisions.csv"
CND_CSV  = "/tmp/orb_candles_fyers.csv"
HORIZONS = [15, 30, 60]            # minutes
SESS_END = dtime(15, 30)
GATE_THR = 0.20
BUCKETS  = [(0,0.10),(0.10,0.15),(0.15,0.20),(0.20,0.25),(0.25,0.35),(0.35,0.50),(0.50,99)]


def gate_blocks(consol, brk, decision):
    if consol >= GATE_THR:
        return False
    if brk == "NONE":
        return True
    if decision == "BUY"  and brk == "BREAKOUT_LOW":
        return True
    if decision == "SELL" and brk == "BREAKOUT_HIGH":
        return True
    return False


def load_candles():
    c = pd.read_csv(CND_CSV, parse_dates=["ts_ist"]).rename(columns={"ts_ist":"ts"})
    c = c[c["ts"] >= pd.Timestamp("2026-03-25")]          # decisions start Apr; trim
    idx = {}
    for sym, g in c.groupby("symbol"):
        g = g.sort_values("ts")
        idx[sym] = (g["ts"].to_numpy(), g["close"].to_numpy(float),
                    g["ts"].dt.date.to_numpy())
    return idx


def fwd_close(idx, sym, t, h):
    """Close of first bar at/after t+h, same calendar day; else EOD close."""
    if sym not in idx:
        return None
    ts, cl, dates = idx[sym]
    target = np.datetime64(t + timedelta(minutes=h))
    i = np.searchsorted(ts, target, side="left")
    day = t.date()
    if i >= len(ts) or dates[i] != day:               # past day's end -> use last same-day bar
        same = np.where(dates == day)[0]
        return cl[same[-1]] if len(same) else None
    return cl[i]


def entry_close(idx, sym, t):
    ts, cl, dates = idx[sym]
    i = np.searchsorted(ts, np.datetime64(t), side="left")
    if i >= len(ts) or dates[i] != t.date():
        return None
    return cl[i]


def summarise(df, col):
    a = df[col].dropna()
    if not len(a):
        return "      n=0"
    return (f"n={len(a):>4}  win={ (a>0).mean()*100:>4.0f}%  "
            f"avg={a.mean():>+6.3f}%  median={a.median():>+6.3f}%")


def main():
    d = pd.read_csv(DEC_CSV, parse_dates=["t"])
    d = d[(d["t"].dt.time >= dtime(9,30)) & (d["t"].dt.time <= SESS_END)]
    idx = load_candles()

    for h in HORIZONS:
        rets = []
        for r in d.itertuples():
            p0 = entry_close(idx, r.symbol, r.t)
            p1 = fwd_close(idx, r.symbol, r.t, h)
            if p0 is None or p1 is None or p0 <= 0:
                rets.append(np.nan); continue
            ret = (p1-p0)/p0*100
            rets.append(ret if r.decision == "BUY" else -ret)
        d[f"fwd{h}"] = rets

    d["blocked"] = [gate_blocks(c, b, dec) for c,b,dec in zip(d["consol"], d["brk"], d["decision"])]
    d["nobrk"]   = d["brk"] == "NONE"

    print(f"Decisions scored: {len(d)}  (BUY {sum(d.decision=='BUY')} / SELL {sum(d.decision=='SELL')})")
    print(f"Span: {d['t'].min():%Y-%m-%d} .. {d['t'].max():%Y-%m-%d}")

    H = 30
    col = f"fwd{H}"
    print(f"\n{'='*72}\n  FORWARD {H}-MIN DIRECTIONAL MOVE  (win = signal was right)\n{'='*72}")
    print(f"  ALL signals          : {summarise(d, col)}")

    print(f"\n  ── 1) IS THE GATE NEEDED?  (gate's block vs allow set) ──")
    print(f"    Gate BLOCKS          : {summarise(d[d.blocked], col)}")
    print(f"    Gate ALLOWS          : {summarise(d[~d.blocked], col)}")

    print(f"\n  ── 2) THRESHOLD: forward move by consolidation_pct bucket ──")
    print(f"     (no-breakout signals only — the case the gate keys on)")
    nb = d[d.nobrk]
    print(f"     {'consol_pct':<14}{'30-min fwd':<34}{'15m win':>8}{'60m win':>8}")
    for lo, hi in BUCKETS:
        b = nb[(nb["consol"] >= lo) & (nb["consol"] < hi)]
        if not len(b):
            continue
        w15 = (b['fwd15'].dropna()>0).mean()*100 if b['fwd15'].notna().any() else float('nan')
        w60 = (b['fwd60'].dropna()>0).mean()*100 if b['fwd60'].notna().any() else float('nan')
        label = f"{lo:.2f}-{hi:.2f}" if hi < 90 else f">{lo:.2f}"
        print(f"     {label:<14}{summarise(b, col):<34}{w15:>7.0f}%{w60:>7.0f}%")

    print(f"\n  ── 3) breakout signals (gate ALLOWS these even when tight) ──")
    for name, mask in [("BREAKOUT aligned",
                        ((d.brk=="BREAKOUT_HIGH")&(d.decision=="BUY"))|((d.brk=="BREAKOUT_LOW")&(d.decision=="SELL"))),
                       ("BREAKOUT against",
                        ((d.brk=="BREAKOUT_HIGH")&(d.decision=="SELL"))|((d.brk=="BREAKOUT_LOW")&(d.decision=="BUY")))]:
        print(f"    {name:<21}: {summarise(d[mask], col)}")


if __name__ == "__main__":
    main()
