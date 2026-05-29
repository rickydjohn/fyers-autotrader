#!/usr/bin/env python3
"""
ORB false-break study.

Question (Ricky, 2026-05-29): when the market breaks the opening range and then
re-enters it ("false break" / false sense of direction), how DEEP does the poke
go as a fraction of the ORB range width — and how often does it happen? The goal
is a *sensible*, range-relative confirmation buffer, replacing today's fixed
0.20%-of-price ORB_BUFFER.

Definitions
-----------
* Opening range : high/low of 09:15-09:29 IST 1m bars  (matches backtest_orb_gate.fetch_orb)
* width         : orb_high - orb_low
* Break         : after 09:30, a 1m bar trades beyond a RAW boundary
                  (bar.high > orb_high  ->  UP break ;  bar.low < orb_low  ->  DOWN break).
                  Measured raw, NOT buffered — the buffer is what we're calibrating.
* Penetration   : max distance the bar extreme reaches beyond the boundary
                  before re-entry  (UP: max(high)-orb_high ; DOWN: orb_low-min(low)).
* Re-entry      : first bar whose CLOSE comes back inside [orb_low, orb_high].
                  (close-based avoids a single wick bar registering as instant re-entry.)
* False break   : breaks AND re-enters within REENTRY_WINDOW_MIN (default 30 min).
                  Holding outside >30 min, or never re-entering = TRUE break.
* Metric        : penetration / width  (e.g. 10 pts on a 100-pt range = 10%).

Data: local CSV pulled from TimescaleDB market_candles (1m bars), see header of
this repo's investigation. Only the clean 1m era (~Feb-May 2026) survives the
per-day quality filter; coarse older months are excluded automatically.

Run:  python tests/backtests/backtest_orb_false_breaks.py [csv_path]
"""

import sys
from datetime import time as dtime

import numpy as np
import pandas as pd

CSV_PATH           = sys.argv[1] if len(sys.argv) > 1 else "/tmp/orb_candles.csv"
REENTRY_WINDOW_MIN = 30          # break is "false" only if it re-enters within this many minutes
OPEN_START         = dtime(9, 15)
OPEN_END           = dtime(9, 30)   # exclusive — opening range is 09:15..09:29
SESSION_END        = dtime(15, 30)
MIN_OPENING_BARS   = 10          # need most of the 15 opening minutes
MIN_DAY_BARS       = 200         # ensures 1m-era full-coverage day (drops 5-8 bar/day months)
CURRENT_BUFFER_PCT = 0.002       # deployed ORB_BUFFER = 0.20% of price
SYMBOLS            = ["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX"]
CANDIDATE_BUFFERS  = [5, 10, 15, 20, 25, 30, 40, 50]   # candidate buffers as % of ORB width


def detect_breaks(post, orb_high, orb_low, width):
    """State-machine scan of post-09:30 bars. Returns list of break-event dicts.

    post: list of row-dicts {ts, open, high, low, close} sorted ascending by ts.
    """
    events = []
    n = len(post)
    i = 0
    first_flagged = False
    while i < n:
        bar = post[i]
        broke_up   = bar["high"] > orb_high
        broke_down = bar["low"]  < orb_low
        if not (broke_up or broke_down):
            i += 1
            continue

        if broke_up and broke_down:                       # straddle bar -> deeper side wins
            side = "UP" if (bar["high"] - orb_high) >= (orb_low - bar["low"]) else "DOWN"
        else:
            side = "UP" if broke_up else "DOWN"

        t_break = bar["ts"]
        extreme = bar["high"] if side == "UP" else bar["low"]

        j = i
        reentered, t_reentry, reentry_idx = False, None, None
        while j < n:
            b = post[j]
            if side == "UP":
                extreme = max(extreme, b["high"])
                if b["close"] <= orb_high:
                    reentered, t_reentry, reentry_idx = True, b["ts"], j
                    break
            else:
                extreme = min(extreme, b["low"])
                if b["close"] >= orb_low:
                    reentered, t_reentry, reentry_idx = True, b["ts"], j
                    break
            j += 1

        penetration = (extreme - orb_high) if side == "UP" else (orb_low - extreme)
        if reentered:
            time_outside = (t_reentry - t_break).total_seconds() / 60.0
            is_false = time_outside <= REENTRY_WINDOW_MIN
        else:
            time_outside, is_false = None, False

        events.append({
            "side":          side,
            "t_break":       t_break,
            "penetration":   penetration,
            "pen_pct_width": penetration / width * 100.0 if width > 0 else np.nan,
            "pen_pct_price": penetration / ((orb_high + orb_low) / 2) * 100.0,
            "time_outside":  time_outside,
            "reentered":     reentered,
            "is_false":      is_false,
            "is_first":      not first_flagged,
        })
        first_flagged = True
        if reentered:
            i = reentry_idx + 1
        else:
            break
    return events


def pctl(arr, q):
    return float(np.percentile(arr, q)) if len(arr) else float("nan")


def analyse_symbol(df_sym):
    days_used, days_excluded = 0, 0
    all_events, day_meta = [], []

    for day, g in df_sym.groupby(df_sym["ts"].dt.date):
        g = g.sort_values("ts")
        op = g[(g["ts"].dt.time >= OPEN_START) & (g["ts"].dt.time < OPEN_END)]
        intraday = g[(g["ts"].dt.time >= OPEN_START) & (g["ts"].dt.time <= SESSION_END)]
        if len(op) < MIN_OPENING_BARS or len(intraday) < MIN_DAY_BARS:
            days_excluded += 1
            continue

        orb_high = float(op["high"].max())
        orb_low  = float(op["low"].min())
        width    = orb_high - orb_low
        if width <= 0:
            days_excluded += 1
            continue

        post = intraday[intraday["ts"].dt.time >= OPEN_END][
            ["ts", "open", "high", "low", "close"]
        ].to_dict("records")
        events = detect_breaks(post, orb_high, orb_low, width)
        for e in events:
            e["day"] = day
        all_events.extend(events)
        day_meta.append({
            "day": day, "width": width,
            "width_pct_price": width / ((orb_high + orb_low) / 2) * 100.0,
            "had_break": len(events) > 0,
        })
        days_used += 1

    return days_used, days_excluded, all_events, pd.DataFrame(day_meta)


def report(symbol, days_used, days_excluded, events, day_meta):
    ev = pd.DataFrame(events)
    print(f"\n{'='*78}\n  {symbol}\n{'='*78}")
    print(f"  Usable 1m days        : {days_used}   (excluded for coarse/partial data: {days_excluded})")
    if ev.empty:
        print("  No break events.")
        return

    breaks_days = int(day_meta["had_break"].sum())
    print(f"  Days with ≥1 ORB break: {breaks_days} / {days_used} "
          f"({breaks_days/days_used*100:.0f}%)")
    print(f"  Median ORB width      : {day_meta['width'].median():.0f} pts "
          f"({day_meta['width_pct_price'].median():.2f}% of price)")

    first = ev[ev["is_first"]]
    print(f"\n  ── FIRST break of the day (what the gate reacts to) ──")
    nf, ntot = int(first["is_false"].sum()), len(first)
    print(f"    First breaks classified : {ntot}")
    print(f"    FALSE (re-enter ≤{REENTRY_WINDOW_MIN}m): {nf}  ({nf/ntot*100:.0f}%)")
    print(f"    TRUE  (held / followed) : {ntot-nf}  ({(ntot-nf)/ntot*100:.0f}%)")

    print(f"\n  ── ALL break events ──")
    nf_all, ntot_all = int(ev["is_false"].sum()), len(ev)
    print(f"    Total break excursions  : {ntot_all}")
    print(f"    FALSE breaks            : {nf_all}  ({nf_all/ntot_all*100:.0f}%)")

    # Penetration depth of FALSE breaks, as % of ORB width — the headline number.
    fb = ev[ev["is_false"]]
    pw = fb["pen_pct_width"].to_numpy()
    pp = fb["pen_pct_price"].to_numpy()
    to = fb["time_outside"].dropna().to_numpy()
    print(f"\n  ── FALSE-break penetration depth  (penetration ÷ ORB width) ──")
    print(f"    n false breaks : {len(pw)}")
    for q in (50, 75, 90, 95):
        print(f"    p{q:<3d}          : {pctl(pw, q):5.1f}% of width   "
              f"(= {pctl(pp, q):.3f}% of price)")
    print(f"    mean           : {np.nanmean(pw):5.1f}% of width   "
          f"(= {np.nanmean(pp):.3f}% of price)")
    print(f"    max            : {np.nanmax(pw):5.1f}% of width")
    print(f"    median time outside before re-entry: {np.median(to):.0f} min")

    # Most false breaks are sub-minute wick rejections (a buffer of even a few %
    # filters those). The ones that genuinely fake out direction persist for
    # several minutes — characterise those separately.
    print(f"\n  ── 'Meaningful' false breaks (persisted before re-entry) ──")
    for mins in (3, 5):
        m = fb[fb["time_outside"] >= mins]
        mpw = m["pen_pct_width"].to_numpy()
        share = len(m) / len(fb) * 100 if len(fb) else float("nan")
        if len(mpw):
            print(f"    ≥{mins}m outside: n={len(m):3d} ({share:2.0f}% of false)  "
                  f"depth p50/p75/p90 = {pctl(mpw,50):.0f}/{pctl(mpw,75):.0f}/{pctl(mpw,90):.0f}% of width")
        else:
            print(f"    ≥{mins}m outside: none")

    # How does the DEPLOYED 0.20%-of-price buffer fare?
    cur_filtered = int((fb["pen_pct_price"] < CURRENT_BUFFER_PCT * 100).sum())
    # The fixed %-of-price buffer becomes a different fraction of the ORB width
    # every day depending on how wide the range opened — that inconsistency is
    # the core argument for a range-relative buffer.
    cur_as_width = (CURRENT_BUFFER_PCT * 100 / day_meta["width_pct_price"]) * 100
    print(f"\n  ── Deployed buffer = {CURRENT_BUFFER_PCT*100:.2f}% of price ──")
    print(f"    In ORB-width terms it ranges : p25={pctl(cur_as_width,25):.0f}%  "
          f"median={pctl(cur_as_width,50):.0f}%  p75={pctl(cur_as_width,75):.0f}%  "
          f"(swings {pctl(cur_as_width,75)/pctl(cur_as_width,25):.1f}× day-to-day)")
    print(f"    False breaks it would filter : {cur_filtered}/{len(fb)} "
          f"({cur_filtered/len(fb)*100:.0f}%)")

    # Candidate range-relative buffers: filter rate vs cost.
    print(f"\n  ── Candidate buffer = X% of ORB width ──")
    print(f"    {'buffer':>8} {'false filtered':>15} {'true delayed*':>14}  ~equiv %price")
    tb = ev[(~ev["is_false"]) & ev["reentered"].notna()]  # true breaks (incl. never-reenter)
    true_pen = ev[~ev["is_false"]]["pen_pct_width"].to_numpy()
    for b in CANDIDATE_BUFFERS:
        ffilt = (pw < b).mean() * 100 if len(pw) else float("nan")
        # a true break still has to clear the buffer before entry — "delayed" if its
        # eventual penetration exceeds b (it always enters, just later/worse).
        tdel = (true_pen >= b).mean() * 100 if len(true_pen) else float("nan")
        eq_price = (b/100 * day_meta["width_pct_price"]).median()
        print(f"    {b:>6}% {ffilt:>14.0f}% {tdel:>13.0f}%  {eq_price:>10.3f}%")
    print("    *true breaks are never blocked, only entered after the buffer is cleared")


def main():
    df = pd.read_csv(CSV_PATH, parse_dates=["ts_ist"]).rename(columns={"ts_ist": "ts"})
    df = df[(df["ts"].dt.time >= OPEN_START) & (df["ts"].dt.time <= SESSION_END)]
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)

    print(f"Loaded {len(df):,} intraday 1m bars from {CSV_PATH}")
    print(f"Re-entry window for 'false' = {REENTRY_WINDOW_MIN} min")

    for sym in SYMBOLS:
        d = df[df["symbol"] == sym]
        if d.empty:
            print(f"\n  (no rows for {sym})")
            continue
        used, excl, events, day_meta = analyse_symbol(d)
        report(sym, used, excl, events, day_meta)


if __name__ == "__main__":
    main()
