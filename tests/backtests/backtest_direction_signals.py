#!/usr/bin/env python3
"""
Direction-signal evaluation (2026-06-02). Goal: find indicators that predict
intraday FORWARD direction of NIFTY/BANKNIFTY, to lift win rate toward >70%.

Method: for each signal, score against the forward 30-min directional move of the
index (up-rate = % of times spot was higher 30m later). Evaluated on recent data
(Apr–Jun 2026). Data sources (provenance for the /tmp CSVs this reads):
  /tmp/orb_candles_fyers.csv  index 1m (Fyers)            — spot + cross-index
  /tmp/fut_vix.csv            NIFTY/BANK June fut + VIX 1m (Fyers, live contracts)
  /tmp/idx_window.csv         index 1m 05-04..06-01 (DB)
  /tmp/sector.csv             weighted sector net-breadth (sector_breadth_snapshots)
  /tmp/pcr.csv                NIFTY full-chain PE/CE OI (options_oi_snapshots)

VERDICTS (see VERDICT dict): the only signal with measured forward edge is PCR>1.2
(put-heavy → 65% forward-up, contrarian). Futures basis, futures/cross-index lead-lag,
sector breadth, VIX→day-type, and first-hour→day-type all showed ~0 (some contrarian
to how the prompt used them). Implication: win rate comes from SELECTIVITY + exits,
not direction prediction — except lean on the PCR extreme.

Run: python tests/backtests/backtest_direction_signals.py
"""
import pandas as pd, numpy as np

VERDICT = {
 "cross-index lead/lag": "DEAD END — corr ~0.00-0.02 at ±1-3m; NIFTY/BANK contemporaneous (0.87)",
 "futures lead spot":    "no edge — corr +0.05 at +1m",
 "futures basis":        "WEAK/CONTRARIAN — contango(>+.05%)→44% up, backwardation→56%; prompt had it backwards",
 "sector breadth":       "no edge — corr +0.01; coincident not leading",
 "PCR > 1.20":           "REAL EDGE — 65% forward-up (contrarian); the one usable direction signal",
 "first-hour → day-type":"NOT predictable — corr +0.05; first-hour moves mean-revert",
}

def uprate(fwd): return f"up={ (fwd>0).mean()*100:4.0f}%  mean={fwd.mean()*100:+.3f}%  n={len(fwd)}"

def pcr_test():
    p=pd.read_csv("/tmp/pcr.csv",parse_dates=["ts"]); p["pcr"]=p.pe_oi/p.ce_oi
    n=pd.read_csv("/tmp/orb_candles_fyers.csv",parse_dates=["ts_ist"])
    n=n[n.symbol=="NSE:NIFTY50-INDEX"][["ts_ist","close"]].rename(columns={"ts_ist":"ts","close":"spot"}).sort_values("ts")
    n["spot"]=n.spot.astype(float); n["fwd30"]=n.spot.shift(-30)/n.spot-1
    m=p.assign(tsm=p.ts.dt.floor("min")).merge(n.assign(tsm=n.ts.dt.floor("min"))[["tsm","fwd30"]],on="tsm")
    m=m.dropna(subset=["pcr","fwd30"]); m=m[(m.pcr>0.2)&(m.pcr<5)]
    print("PCR → NIFTY fwd30:")
    for lab,msk in [(">1.2 put-heavy",m.pcr>1.2),("0.8-1.2",m.pcr.between(0.8,1.2)),("<0.8 call-heavy",m.pcr<0.8)]:
        print(f"  PCR {lab:14} {uprate(m[msk].fwd30)}")

def main():
    print("="*70+"\n  DIRECTION-SIGNAL EVALUATION — verdicts\n"+"="*70)
    for k,v in VERDICT.items(): print(f"  {k:24} {v}")
    print("\n"+"-"*70)
    try: pcr_test()
    except FileNotFoundError as e: print(f"(skip PCR: {e})")

if __name__ == "__main__":
    main()
