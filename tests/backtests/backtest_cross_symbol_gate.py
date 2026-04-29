"""
Backtest: cross-symbol confidence gate (Layer 2).

Retroactively applies the NIFTY → BANKNIFTY gate to historical trade data and
reports what win-rate improvement (or regression) would have occurred.

Logic:
  For each BANKNIFTY closed trade:
    1. Find the NIFTY decision made within PEER_WINDOW_MINUTES before the trade's
       BANKNIFTY decision time.
    2. If NIFTY decision conflicts (BUY vs SELL) → trade would have been BLOCKED.
    3. If NIFTY decision aligns → trade would have been BOOSTED (confidence +0.08),
       but not blocked — no change to whether we traded.

Run from project root:
    python3 backtest_cross_symbol_gate.py
"""

import json
import sys
from datetime import datetime, timedelta
from typing import Optional

import psycopg2
import psycopg2.extras
import pytz

DB = dict(host="localhost", port=5432, dbname="trading", user="trading", password="trading")
IST = pytz.timezone("Asia/Kolkata")
PEER_WINDOW_MINUTES = 15
NIFTY_SYM   = "NSE:NIFTY50-INDEX"
BNIFTY_SYM  = "NSE:NIFTYBANK-INDEX"


def connect():
    return psycopg2.connect(**DB)


def fetch_nifty_decisions(cur) -> list[dict]:
    """All non-HOLD NIFTY decisions, ordered by time."""
    cur.execute(
        """
        SELECT decision_id, time AT TIME ZONE 'Asia/Kolkata' AS time_ist,
               decision, confidence
        FROM ai_decisions
        WHERE symbol = %s AND decision != 'HOLD'
        ORDER BY time ASC
        """,
        (NIFTY_SYM,),
    )
    return [
        {
            "decision_id": r[0],
            "time": r[1],
            "decision": r[2],
            "confidence": float(r[3]),
        }
        for r in cur.fetchall()
    ]


def fetch_banknifty_trades(cur) -> list[dict]:
    """
    All closed BANKNIFTY trades, identified via the originating decision's symbol.
    Trades use option symbols (e.g. NSE:BANKNIFTY26APR52700CE) not the index symbol,
    so we join on ai_decisions.symbol to filter by underlying.
    """
    cur.execute(
        """
        SELECT t.trade_id,
               t.side,
               t.pnl,
               t.entry_time AT TIME ZONE 'Asia/Kolkata' AS entry_ist,
               t.exit_reason,
               t.status,
               d.time    AT TIME ZONE 'Asia/Kolkata' AS decision_ist,
               d.decision AS decision_text,
               d.confidence AS decision_confidence
        FROM trades t
        JOIN ai_decisions d ON d.decision_id = t.decision_id
        WHERE d.symbol = %s
          AND t.status IN ('CLOSED', 'STOPPED')
          AND t.pnl IS NOT NULL
        ORDER BY t.entry_time ASC
        """,
        (BNIFTY_SYM,),
    )
    return [
        {
            "trade_id":             r[0],
            "side":                 r[1],
            "pnl":                  float(r[2]),
            "entry_ist":            r[3],
            "exit_reason":          r[4],
            "status":               r[5],
            "decision_time":        r[6],
            "decision":             r[7],
            "decision_confidence":  float(r[8]) if r[8] else None,
        }
        for r in cur.fetchall()
    ]


def find_peer_decision(
    nifty_decisions: list[dict],
    banknifty_decision_time: datetime,
) -> Optional[dict]:
    """
    Return the most-recent NIFTY decision that was made within
    PEER_WINDOW_MINUTES before banknifty_decision_time.
    """
    window_start = banknifty_decision_time - timedelta(minutes=PEER_WINDOW_MINUTES)
    candidates = [
        d for d in nifty_decisions
        if window_start <= d["time"] <= banknifty_decision_time
    ]
    return candidates[-1] if candidates else None  # most recent in window


def fmt_pct(n, total):
    return f"{n/total*100:.1f}%" if total else "—"


def run():
    try:
        conn = connect()
    except Exception as e:
        print(f"DB connection failed: {e}")
        print("Make sure TimescaleDB is accessible on localhost:5432")
        sys.exit(1)

    with conn.cursor() as cur:
        nifty_decisions  = fetch_nifty_decisions(cur)
        banknifty_trades = fetch_banknifty_trades(cur)

    conn.close()

    if not banknifty_trades:
        print("No closed BANKNIFTY trades found in DB.")
        sys.exit(0)

    print("=" * 65)
    print("Cross-symbol gate backtest")
    print(f"  NIFTY decisions available : {len(nifty_decisions)}")
    print(f"  BANKNIFTY trades to test  : {len(banknifty_trades)}")
    print(f"  Peer window               : {PEER_WINDOW_MINUTES} min")
    print("=" * 65)

    # ── Classify each trade ───────────────────────────────────────────────────
    no_peer     = []   # NIFTY had no decision in window
    aligned     = []   # NIFTY agreed → would have been boosted (still traded)
    conflicted  = []   # NIFTY disagreed → would have been BLOCKED

    for t in banknifty_trades:
        dt = t["decision_time"]
        if dt is None:
            # No decision_id linked — can't gate; treat as no_peer
            no_peer.append(t)
            continue

        peer = find_peer_decision(nifty_decisions, dt)
        if peer is None:
            no_peer.append(t)
            t["_peer"] = None
            continue

        t["_peer"] = peer
        trade_dir = t["decision"]  # BUY or SELL as decided for BANKNIFTY
        peer_dir  = peer["decision"]

        if trade_dir and peer_dir and peer_dir != trade_dir:
            conflicted.append(t)
        else:
            aligned.append(t)

    # ── Win-rate helper ───────────────────────────────────────────────────────
    def stats(trades, label):
        if not trades:
            return f"  {label:20s}: no trades"
        wins   = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        net    = sum(t["pnl"] for t in trades)
        avg_w  = sum(t["pnl"] for t in wins)   / len(wins)   if wins   else 0
        avg_l  = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
        return (
            f"  {label:20s}: {len(trades):3d} trades | "
            f"wins={len(wins):3d} ({fmt_pct(len(wins), len(trades))}) | "
            f"losses={len(losses):3d} | "
            f"net={net:+.0f} | avg_win={avg_w:+.0f} avg_loss={avg_l:+.0f}"
        )

    print("\n── Baseline (all BANKNIFTY trades) ──────────────────────────────")
    print(stats(banknifty_trades, "All trades"))

    print("\n── After applying cross-symbol gate ─────────────────────────────")
    print(stats(no_peer,    "No NIFTY peer    "))
    print(stats(aligned,    "NIFTY aligned    "))
    print(stats(conflicted, "NIFTY conflicted "))

    # Gated result = no_peer + aligned (conflicted trades would be blocked)
    gated = no_peer + aligned
    print()
    print(stats(gated, "Gated (no+aligned)"))

    # ── Impact summary ────────────────────────────────────────────────────────
    baseline_wins = [t for t in banknifty_trades if t["pnl"] > 0]
    gated_wins    = [t for t in gated if t["pnl"] > 0]
    conf_wins  = [t for t in conflicted if t["pnl"] > 0]
    conf_loss  = [t for t in conflicted if t["pnl"] <= 0]
    net_baseline = sum(t["pnl"] for t in banknifty_trades)
    net_gated    = sum(t["pnl"] for t in gated)

    base_wr   = len(baseline_wins) / len(banknifty_trades) if banknifty_trades else 0
    gated_wr  = len(gated_wins)    / len(gated)            if gated           else 0

    print("\n── Impact ───────────────────────────────────────────────────────")
    print(f"  Baseline win rate   : {base_wr*100:.1f}%  (net P&L: ₹{net_baseline:+.0f})")
    print(f"  Gated win rate      : {gated_wr*100:.1f}%  (net P&L: ₹{net_gated:+.0f})")
    delta_wr  = (gated_wr - base_wr) * 100
    delta_pnl = net_gated - net_baseline
    print(f"  Δ win rate          : {delta_wr:+.1f}pp")
    print(f"  Δ net P&L           : ₹{delta_pnl:+.0f}")
    print(f"  Blocked trades      : {len(conflicted)}  "
          f"(would-be wins={len(conf_wins)}, would-be losses={len(conf_loss)})")

    if conflicted:
        print("\n── Blocked trades detail ────────────────────────────────────────")
        for t in conflicted:
            peer = t["_peer"]
            outcome = "WIN" if t["pnl"] > 0 else "LOSS"
            print(
                f"  {t['trade_id'][:16]}  "
                f"entry={t['entry_ist'].strftime('%m-%d %H:%M') if t['entry_ist'] else '?'}  "
                f"dir={t['decision'] or '?'}  "
                f"peer_dir={peer['decision']}  "
                f"pnl={t['pnl']:+.0f}  exit={t['exit_reason'] or t['status']}  [{outcome}]"
            )

    print("=" * 65)


if __name__ == "__main__":
    run()
