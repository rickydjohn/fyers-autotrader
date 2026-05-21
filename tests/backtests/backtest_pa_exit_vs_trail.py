#!/usr/bin/env python3
"""
PA_SUPPORT / PA_RESISTANCE: outright exit vs trail.

Current behavior in execution/exit_rules.py: when underlying is within 0.25%
of nearest support (for PE) or resistance (for CE) AND position is in profit,
exit immediately ("lock in profit").

Hypothesis under test: trailing instead of exiting captures more on momentum
days while still bounding give-back on bounce days.

Method:
  For each historical PA_SUPPORT / PA_RESISTANCE trade:
    1. Look up underlying price at entry_time and exit_time.
    2. Project forward 60 min of 1m underlying bars (or until session close,
       whichever is first).
    3. Approximate the option premium at each minute using delta≈0.5 for ATM:
         PE premium = entry_option_price + (entry_underlying - underlying_t) * 0.5
         CE premium = entry_option_price + (underlying_t - entry_underlying) * 0.5
       (Ignores theta decay and IV changes — fine over a 60-min horizon.)
    4. Simulate a trail starting at peak = exit-time premium:
         - track peak as max of projected premiums going forward
         - exit when premium < peak * (1 - trail_offset)
         - or at the 60-min mark / session close, exit at current premium
    5. Compare hypothetical trail P&L to actual PA exit P&L.

Tests three trail offsets: 2%, 3%, 5%.

Run inside trading-data container:
    docker cp tests/backtests/backtest_pa_exit_vs_trail.py trading-data:/tmp/
    docker exec trading-data python /tmp/backtest_pa_exit_vs_trail.py
"""

import asyncio
from datetime import datetime, time as dtime, timedelta

import asyncpg
import pytz

DB_DSN = "postgresql://trading:trading@timescaledb:5432/trading"
IST    = pytz.timezone("Asia/Kolkata")

DELTA_APPROX  = 0.5             # ATM option delta — used to project premium
LOOKFWD_MIN   = 60              # how many minutes forward to simulate
SESSION_CLOSE = dtime(15, 20)   # matches settings.session_close_*

TRAIL_OFFSETS = [0.02, 0.03, 0.05]   # peak × (1 - offset) = trail exit floor


def _under_symbol(opt_symbol: str) -> str:
    if "BANKNIFTY" in opt_symbol:
        return "NSE:NIFTYBANK-INDEX"
    return "NSE:NIFTY50-INDEX"


async def fetch_underlying_at(conn, symbol: str, ts) -> float | None:
    row = await conn.fetchrow("""
        SELECT close FROM market_candles
        WHERE symbol = $1 AND time <= $2
        ORDER BY time DESC LIMIT 1
    """, symbol, ts)
    return float(row["close"]) if row else None


async def fetch_underlying_forward(conn, symbol: str, start, minutes: int):
    """Return [(ts_utc, close)] of 1m bars from start through start+minutes."""
    end = start + timedelta(minutes=minutes)
    rows = await conn.fetch("""
        SELECT time, close FROM market_candles
        WHERE symbol = $1 AND time > $2 AND time <= $3
        ORDER BY time
    """, symbol, start, end)
    return [(r["time"], float(r["close"])) for r in rows]


def project_premium(side: str, entry_underlying: float, entry_premium: float,
                    underlying_now: float) -> float:
    if side == "SELL":   # PE — premium goes UP when underlying goes DOWN
        return entry_premium + (entry_underlying - underlying_now) * DELTA_APPROX
    return entry_premium + (underlying_now - entry_underlying) * DELTA_APPROX


def simulate_trail(side: str, entry_underlying: float, entry_premium: float,
                   exit_premium_actual: float, forward, offset: float) -> float:
    """Return the trail-exit premium given the forward 1m underlying bars."""
    peak = exit_premium_actual    # we entered the trail above this
    last_premium = exit_premium_actual

    for ts, under in forward:
        ts_ist = ts.astimezone(IST)
        # Stop at session close (force-close all positions there)
        if ts_ist.time() >= SESSION_CLOSE:
            return last_premium
        prem = project_premium(side, entry_underlying, entry_premium, under)
        last_premium = prem
        if prem > peak:
            peak = prem
        if prem < peak * (1 - offset):
            return prem    # trail-floor hit
    # No floor hit within lookforward window — exit at last observed
    return last_premium


async def main():
    conn = await asyncpg.connect(DB_DSN)
    trades = await conn.fetch("""
        SELECT trade_id, symbol, side, quantity,
               entry_time, exit_time,
               entry_price, exit_price,
               option_type, option_symbol, exit_reason
        FROM trades
        WHERE exit_reason IN ('PA_SUPPORT', 'PA_RESISTANCE')
          AND exit_price > 0 AND entry_price > 0
        ORDER BY entry_time
    """)

    results = []   # list of dicts per trade

    for t in trades:
        under_sym = _under_symbol(t["option_symbol"] or t["symbol"])
        entry_under = await fetch_underlying_at(conn, under_sym, t["entry_time"])
        exit_under  = await fetch_underlying_at(conn, under_sym, t["exit_time"])
        if entry_under is None or exit_under is None:
            continue

        forward = await fetch_underlying_forward(conn, under_sym, t["exit_time"], LOOKFWD_MIN)
        if not forward:
            continue

        entry_prem = float(t["entry_price"])
        exit_prem  = float(t["exit_price"])
        qty = int(t["quantity"])

        actual_premium_diff = exit_prem - entry_prem
        if t["side"] == "SELL":   # PE
            # premium increase when underlying drops — captured by exit > entry
            actual_premium_diff = exit_prem - entry_prem  # already directional via project_premium

        row = {
            "trade_id": t["trade_id"],
            "side": t["side"],
            "qty": qty,
            "entry_premium": entry_prem,
            "exit_premium_actual": exit_prem,
            "actual_gain_per_share": exit_prem - entry_prem,
        }
        for offset in TRAIL_OFFSETS:
            trail_exit = simulate_trail(t["side"], entry_under, entry_prem,
                                        exit_prem, forward, offset)
            row[f"trail_exit_{int(offset*100)}pct"] = trail_exit
            row[f"trail_gain_per_share_{int(offset*100)}pct"] = trail_exit - entry_prem
        results.append(row)

    await conn.close()

    # ── report ─────────────────────────────────────────────────────────
    print("=" * 90)
    print(f"PA exit vs trail backtest — {len(results)} trades")
    print(f"Forward window: {LOOKFWD_MIN} min, delta approx: {DELTA_APPROX}, session close: {SESSION_CLOSE}")
    print("=" * 90)

    if not results:
        print("No usable trades.")
        return

    actual_avg = sum(r["actual_gain_per_share"] for r in results) / len(results)
    print(f"\nActual outright-exit avg gain per share: ₹{actual_avg:+.2f}")

    for offset in TRAIL_OFFSETS:
        key = f"trail_gain_per_share_{int(offset*100)}pct"
        trail_avg = sum(r[key] for r in results) / len(results)
        wins  = sum(1 for r in results if r[key] > r["actual_gain_per_share"])
        loses = sum(1 for r in results if r[key] < r["actual_gain_per_share"])
        ties  = len(results) - wins - loses
        delta_per_share = trail_avg - actual_avg

        # Weighted-by-quantity P&L delta (more realistic than per-share avg)
        total_qty_delta = sum(
            (r[key] - r["actual_gain_per_share"]) * r["qty"]
            for r in results
        )

        print(f"\n── Trail offset {int(offset*100)}% ──")
        print(f"  trail avg gain per share: ₹{trail_avg:+.2f}  (Δ vs outright: ₹{delta_per_share:+.2f}/share)")
        print(f"  trades where trail wins:   {wins:>4} / {len(results)}  ({wins/len(results)*100:.1f}%)")
        print(f"  trades where exit wins:    {loses:>4} / {len(results)}  ({loses/len(results)*100:.1f}%)")
        print(f"  ties:                      {ties:>4}")
        print(f"  cumulative ₹ delta (qty-weighted): {total_qty_delta:+,.0f}")

    # Distribution of post-exit movement
    print("\n── Post-exit underlying movement (in trade direction) ──")
    # Quick aggregate: average favorable move over LOOKFWD_MIN
    favorable_moves = []
    for t in trades:
        if t["side"] not in ("BUY", "SELL"):
            continue
        under_sym = _under_symbol(t["option_symbol"] or t["symbol"])
        exit_under = await fetch_underlying_at(conn, under_sym, t["exit_time"]) if False else None
    # Skip — already covered by the per-trade win/loss analysis above.


if __name__ == "__main__":
    asyncio.run(main())
