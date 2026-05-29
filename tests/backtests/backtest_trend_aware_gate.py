#!/usr/bin/env python3
"""
Validate the trend-aware entry-gate relaxation (2026-05-29).

Change under test (simulation-engine/main.py): when an entry is "trend-aligned"
— price already beyond the opening range in the trade's direction (SELL below
orb_low / BUY above orb_high) — skip the proximity gate and don't treat a
PA-trail engagement as a pre-entry block. Counter-trend setups keep both vetoes.

This replays decisions (ai_decisions snapshot) and asks: of the trades the OLD
PA/proximity logic blocked, which does the change UNBLOCK (trend-aligned), and
are they better than the ones still blocked? Outcome = forward underlying move
(directional proxy) + a realistic SL(-0.20%)/target(+0.40%) first-touch race.

Inputs: /tmp/decisions3.csv (with orb_high/orb_low), /tmp/orb_candles_fyers.csv
Run: python tests/backtests/backtest_trend_aware_gate.py
"""
import numpy as np, pandas as pd

DEC="/tmp/decisions3.csv"; CND="/tmp/orb_candles_fyers.csv"
PROX=0.0025; SL=0.0020; TGT=0.0040

c=pd.read_csv(CND,parse_dates=["ts_ist"]); idx={}
for s,g in c.groupby("symbol"):
    g=g.sort_values("ts_ist")
    idx[s]=(g["ts_ist"].to_numpy(),g["high"].to_numpy(float),g["low"].to_numpy(float),g["close"].to_numpy(float),g["ts_ist"].dt.date.to_numpy())

def entry(sym,t):
    if sym not in idx: return None
    ts,_,_,cl,d=idx[sym]; i=np.searchsorted(ts,np.datetime64(t),"left")
    return cl[i] if i<len(ts) and d[i]==pd.Timestamp(t).date() else None

def fret(sym,t,side,mins):
    if sym not in idx: return np.nan
    ts,_,_,cl,d=idx[sym]; t0=np.datetime64(t); P0=entry(sym,t)
    m=(ts>t0)&(ts<=t0+np.timedelta64(mins,'m'))&(d==pd.Timestamp(t).date())
    if P0 is None or m.sum()==0: return np.nan
    pf=cl[m][-1]; r=(pf-P0)/P0*100
    return r if side=="BUY" else -r

def race(sym,t,side,P):
    if sym not in idx: return None
    ts,hi,lo,_,d=idx[sym]; t0=np.datetime64(t); m=(ts>t0)&(d==pd.Timestamp(t).date())
    for h,l in zip(hi[m],lo[m]):
        if side=="SELL":
            if h>=P*(1+SL): return 0
            if l<=P*(1-TGT): return 1
        else:
            if l<=P*(1-SL): return 0
            if h>=P*(1+TGT): return 1
    return None

def pa_blocked(r):
    P=r.price
    if r.decision=="SELL":
        if pd.notna(r.ns) and r.ns>0 and abs(P-r.ns)/r.ns<=PROX: return True
        if pd.notna(r.pdl) and r.pdl>0 and P>r.pdl and abs(P-r.pdl)/r.pdl<=PROX: return True
    else:
        if pd.notna(r.nr) and r.nr>0 and abs(P-r.nr)/r.nr<=PROX: return True
        if pd.notna(r.pdh) and r.pdh>0 and P<r.pdh and abs(P-r.pdh)/r.pdh<=PROX: return True
    return False

d=pd.read_csv(DEC,parse_dates=["t"])
d=d[(d.t.dt.time>=pd.to_datetime("09:30").time())&(d.t.dt.time<=pd.to_datetime("15:30").time())].copy()
d["blocked"]=d.apply(pa_blocked,axis=1)
d["trend_aligned"]=((d.decision=="SELL")&(d.orb_low>0)&(d.price<d.orb_low))|((d.decision=="BUY")&(d.orb_high>0)&(d.price>d.orb_high))
d["f30"]=[fret(r.symbol,r.t,r.decision,30) for r in d.itertuples()]
d["f60"]=[fret(r.symbol,r.t,r.decision,60) for r in d.itertuples()]
d["race"]=[race(r.symbol,r.t,r.decision,r.price) for r in d.itertuples()]
d=d[d.f30.notna()]

def line(name,sub):
    rc=sub.race.dropna()
    print(f"  {name:<42} n={len(sub):>4}  race WIN={rc.mean()*100 if len(rc) else float('nan'):>3.0f}%  "
          f"f60 win={(sub.f60.dropna()>0).mean()*100:>3.0f}%  f60 avg={sub.f60.mean():+.3f}%")

print(f"Decisions with fwd data: {len(d)}   span {d.t.min():%Y-%m-%d}..{d.t.max():%Y-%m-%d}")
for side in ("SELL","BUY"):
    s=d[d.decision==side]; bl=s[s.blocked]
    print(f"\n══ {side} ══  ({len(s)} signals, {len(bl)} PA/proximity-blocked by OLD logic)")
    line("UNBLOCKED by change (blocked & trend-aligned)", bl[bl.trend_aligned])
    line("still blocked (blocked & counter-trend)",       bl[~bl.trend_aligned])
    line("baseline: already-allowed trades",              s[~s.blocked])
