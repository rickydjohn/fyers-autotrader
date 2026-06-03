#!/usr/bin/env python3
"""
Exit-config study (2026-06-01). Controlled trend-vs-range experiment.

Re-simulating historical TRADES on real option PnL is infeasible (Fyers serves no
expired-option history; exit logic also changed mid-May). So instead: take recent
sessions (2026-05-04..06-01, the window where the live June contracts still have
1m history), construct the ATM option at the first ORB-break entry, fetch its REAL
1m path, and sweep exit configs — split by day type — to find a rule that captures
trends without bleeding range days.

Scope: isolates the premium hard-SL + premium-trail (the systemic lever — TRAIL_STOP
is the dominant current-regime exit). Omits the invalidation exit (underlying-based,
fired only 3x ever) and the milestone check (premium trail at +5% fires first). All
configs omit them equally → the comparison is clean.

CAVEAT: NIFTY uses the June-2 weekly for all days, so early-window days hold a longer-
dated (lower-gamma) option than the engine would have traded → option moves muted early.
Trend-vs-range and the relative config ranking still hold; weight conclusions to recent.

Inputs: /tmp/entry_plan.csv, /tmp/opt_window.csv
Run: python tests/backtests/backtest_exit_configs.py
"""
import pandas as pd, numpy as np

plan=pd.read_csv("/tmp/entry_plan.csv")
opt=pd.read_csv("/tmp/opt_window.csv",parse_dates=["ts"])
opt["day"]=opt["ts"].dt.date.astype(str); opt["hm"]=opt["ts"].dt.strftime("%H:%M")
paths={(s,d):list(zip(g.hm,g.close.astype(float))) for (s,d),g in opt.groupby(["opt_symbol","day"])}

def sym_of(r):
    return f"NSE:NIFTY26602{r.strike}{r.side}" if r.idx=="NIFTY" else f"NSE:BANKNIFTY26JUN{r.strike}{r.side}"

VAR=lambda g: 0.05 if g>=0.10 else (0.04 if g>=0.07 else 0.03)   # deployed variable offset
def sim(E,bars,sl,engage,offset_fn,hold=False):
    if not bars: return None
    peak=E
    for _t,px in bars:
        if px<=E*(1-sl): return -sl*100
        peak=max(peak,px)
        if not hold:
            g=peak/E-1
            if g>=engage and px<=peak*(1-offset_fn(g)): return (px-E)/E*100
    return (bars[-1][1]-E)/E*100

# CONVEXITY sweep: entries are coin-flips (no edge), so profit must come from
# lose-small-win-big. Test cutting losers (SL) vs letting winners run (trail/hold).
# Rank by TOTAL (expectancy), not win rate.
CONFIGS={
 "CURRENT (SL15,tight trail)":  dict(sl=0.15,engage=0.05,offset_fn=VAR),
 "HOLD (SL15)":                 dict(sl=0.15,engage=0,offset_fn=VAR,hold=True),
 # implementable: run free, engage a loose protective trail only once a big runner
 "convex eng25/off12 (SL15)":   dict(sl=0.15,engage=0.25,offset_fn=lambda g:0.12),
 "convex eng30/off15 (SL15)":   dict(sl=0.15,engage=0.30,offset_fn=lambda g:0.15),
 "convex eng20/off10 (SL15)":   dict(sl=0.15,engage=0.20,offset_fn=lambda g:0.10),
}

recs=[]
for r in plan.itertuples():
    if r.etime>="14:45": continue                       # no runway after session cutoff
    bars=paths.get((sym_of(r), r.day))
    if not bars: continue
    # entry at first bar >= etime
    after=[(t,px) for t,px in bars if t>=r.etime]
    if len(after)<5: continue
    E=after[0][1]; fwd=after[1:]
    row=dict(day=r.day,idx=r.idx,daytype=r.daytype,side=r.side)
    for name,cfg in CONFIGS.items():
        row[name]=sim(E,fwd,**cfg)
    recs.append(row)

d=pd.DataFrame(recs)
print(f"Trades simulated: {len(d)}  ({d.daytype.value_counts().to_dict()})\n")

def block(title, sub):
    print(f"── {title}  (n={len(sub)}) ──")
    print(f"   {'config':28}{'avg%':>7}{'win%':>6}{'avgWin':>8}{'avgLoss':>9}{'total%':>9}")
    for name in CONFIGS:
        v=sub[name].dropna(); w=v[v>0]; l=v[v<0]
        print(f"   {name:28}{v.mean():>+7.1f}{(v>0).mean()*100:>5.0f}%"
              f"{(w.mean() if len(w) else 0):>+8.1f}{(l.mean() if len(l) else 0):>+9.1f}{v.sum():>+9.0f}")
    print()

block("ALL", d)
block("TREND days", d[d.daytype=="TREND"])
block("RANGE days", d[d.daytype=="RANGE"])
