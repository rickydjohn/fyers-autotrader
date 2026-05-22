#!/usr/bin/env python3
"""
Backtest the pre-entry exit simulation (commit 51541b8).

The shipped gate: before opening, run check_exit on a hypothetical Position
with option_ltp = entry × 1.005. If any exit fires OR PA engages trail (new_milestone=1),
refuse the entry. The intent: never open a position that the exit logic
would immediately want to close.

For historical trades, the only check_exit rule that fires on the hypothetical
is PA_RESISTANCE / PA_SUPPORT (STOP_LOSS doesn't fire above entry × 0.9;
greeks-based rules pass None; trail-floor needs milestone>0). So the backtest
reduces to: would PA proximity have fired at entry?

Method:
  - Pull all option trades with realized P&L (266 trades, 2026-04-07 onward).
  - For each, reconstruct nearest_resistance / nearest_support / PDH / PDL
    from previous-day OHLC + decision-time day_high/day_low. Uses the same
    level-pool logic as get_nearest_levels() in core-engine/indicators/pivots.py.
  - For BUY: check if entry_underlying within 0.25% of nearest_resistance.
  - For SELL: check if entry_underlying within 0.25% of nearest_support or PDL.
  - Classify each trade as WOULD-BE-BLOCKED or WOULD-PASS.

Then compare:
  - Of WOULD-BE-BLOCKED trades, what was their actual P&L?
    - Mostly negative → block was correct (good catch)
    - Mostly positive → block was wrong (missed a winner)
  - Of WOULD-PASS trades, what was their actual P&L?
    - This is the population the new rule lets through.

Run inside trading-data container:
    docker cp tests/backtests/backtest_pre_entry_exit_sim.py trading-data:/tmp/
    docker exec trading-data python /tmp/backtest_pre_entry_exit_sim.py
"""

import asyncio
import json
from datetime import date as _date, timedelta

import asyncpg
import pytz

DB_DSN = "postgresql://trading:trading@timescaledb:5432/trading"
IST    = pytz.timezone("Asia/Kolkata")

PA_PROXIMITY = 0.0025   # ±0.25% (matches PA_RESISTANCE/SUPPORT_PROXIMITY)


def _under_symbol(opt_symbol: str) -> str:
    if "BANKNIFTY" in opt_symbol:
        return "NSE:NIFTYBANK-INDEX"
    return "NSE:NIFTY50-INDEX"


def _compute_pivots(prev_h: float, prev_l: float, prev_c: float) -> dict:
    """Standard daily pivot calculations + extended R4/R5 + S4/S5 + CPR."""
    pp = (prev_h + prev_l + prev_c) / 3.0
    bc = (prev_h + prev_l) / 2.0
    tc = 2 * pp - bc
    r1 = 2 * pp - prev_l
    r2 = pp + (prev_h - prev_l)
    r3 = prev_h + 2 * (pp - prev_l)
    s1 = 2 * pp - prev_h
    s2 = pp - (prev_h - prev_l)
    s3 = prev_l - 2 * (prev_h - pp)
    # extended
    r4 = r3 + (prev_h - prev_l)
    r5 = r4 + (prev_h - prev_l)
    s4 = s3 - (prev_h - prev_l)
    s5 = s4 - (prev_h - prev_l)
    return {
        "Pivot": pp, "CPR-TC": tc, "CPR-BC": bc,
        "R1": r1, "R2": r2, "R3": r3, "R4": r4, "R5": r5,
        "S1": s1, "S2": s2, "S3": s3, "S4": s4, "S5": s5,
    }


def _nearest_levels(price: float, pivots: dict,
                    prev_h: float, prev_l: float,
                    day_h: float, day_l: float) -> tuple:
    """Mirror of pivots.py:get_nearest_levels() — return (nearest_resistance,
    nearest_resistance_label, nearest_support, nearest_support_label)."""
    levels = dict(pivots)
    if prev_h > 0: levels["PDH"] = prev_h
    if prev_l > 0: levels["PDL"] = prev_l
    if day_h > 0: levels["DayHigh"] = day_h
    if day_l > 0: levels["DayLow"] = day_l
    above = {k: v for k, v in levels.items() if v > price}
    below = {k: v for k, v in levels.items() if v <= price}
    nr = min(above.items(), key=lambda x: x[1]) if above else ("None", 0)
    ns = max(below.items(), key=lambda x: x[1]) if below else ("None", 0)
    return nr[1], nr[0], ns[1], ns[0]


def _would_pa_block(side: str, price: float, nr: float, ns: float, pdl: float) -> tuple:
    """Replicate the PA proximity check used in the pre-entry simulation.
    Returns (would_block, reason)."""
    if side == "BUY":
        if nr > 0 and nr * (1 - PA_PROXIMITY) <= price <= nr * (1 + PA_PROXIMITY):
            return True, f"PA_RESISTANCE within 0.25% of nr ₹{nr:.2f}"
    else:   # SELL — also check PDL if underlying still above it
        if ns > 0 and ns * (1 - PA_PROXIMITY) <= price <= ns * (1 + PA_PROXIMITY):
            return True, f"PA_SUPPORT within 0.25% of ns ₹{ns:.2f}"
        if pdl > 0 and price > pdl:
            if pdl * (1 - PA_PROXIMITY) <= price <= pdl * (1 + PA_PROXIMITY):
                return True, f"PA_SUPPORT within 0.25% of PDL ₹{pdl:.2f}"
    return False, ""


async def fetch_prev_day_ohlc(conn, symbol: str, d: _date):
    """Find the most recent trading day's H/L/C before d."""
    row = await conn.fetchrow("""
        SELECT high, low, close FROM daily_ohlcv
        WHERE symbol = $1 AND date < $2
        ORDER BY date DESC LIMIT 1
    """, symbol, d)
    if not row: return None
    return float(row["high"]), float(row["low"]), float(row["close"])


async def fetch_underlying_at(conn, symbol, ts):
    row = await conn.fetchrow("""
        SELECT close FROM market_candles
        WHERE symbol = $1 AND time <= $2
        ORDER BY time DESC LIMIT 1
    """, symbol, ts)
    return float(row["close"]) if row else None


async def main():
    conn = await asyncpg.connect(DB_DSN)
    trades = await conn.fetch("""
        SELECT t.trade_id, t.symbol AS opt_symbol, t.side, t.quantity,
               t.entry_price, t.exit_price, t.entry_time, t.pnl,
               d.indicators_snapshot
        FROM trades t
        JOIN ai_decisions d ON t.decision_id = d.decision_id
        WHERE t.exit_price IS NOT NULL AND t.entry_price > 0
          AND t.option_symbol IS NOT NULL
        ORDER BY t.entry_time
    """)

    blocked = []
    passed  = []

    for t in trades:
        under = _under_symbol(t["opt_symbol"])
        entry_under = await fetch_underlying_at(conn, under, t["entry_time"])
        if entry_under is None:
            continue
        ind = t["indicators_snapshot"]
        if isinstance(ind, str):
            ind = json.loads(ind or "{}")
        day_h = float(ind.get("day_high") or 0)
        day_l = float(ind.get("day_low")  or 0)

        d = t["entry_time"].astimezone(IST).date()
        prev = await fetch_prev_day_ohlc(conn, under, d)
        if not prev:
            continue
        prev_h, prev_l, prev_c = prev
        pivots = _compute_pivots(prev_h, prev_l, prev_c)
        nr, nr_lbl, ns, ns_lbl = _nearest_levels(entry_under, pivots, prev_h, prev_l, day_h, day_l)

        would_block, reason = _would_pa_block(t["side"], entry_under, nr, ns, prev_l)

        rec = {
            "side": t["side"], "qty": int(t["quantity"]),
            "entry_under": entry_under,
            "entry_prem": float(t["entry_price"]),
            "exit_prem":  float(t["exit_price"]),
            "actual_pnl_per_share": float(t["exit_price"]) - float(t["entry_price"]),
            "actual_pnl": float(t["pnl"] or 0),
            "nr": nr, "nr_lbl": nr_lbl, "ns": ns, "ns_lbl": ns_lbl,
            "reason": reason,
        }
        (blocked if would_block else passed).append(rec)

    await conn.close()

    # ── report ─────────────────────────────────────────────────────────
    print("=" * 88)
    print(f"Pre-entry exit-simulation backtest")
    print(f"Total trades: {len(blocked) + len(passed)}  "
          f"(blocked: {len(blocked)},  passed: {len(passed)})")
    print(f"Proximity: ±{PA_PROXIMITY*100:.2f}%")
    print("=" * 88)

    def summarize(label, rows):
        if not rows:
            print(f"\n{label}: 0 trades"); return
        n = len(rows)
        wins  = sum(1 for r in rows if r["actual_pnl"] > 0)
        losses = sum(1 for r in rows if r["actual_pnl"] < 0)
        pnl_sum = sum(r["actual_pnl"] for r in rows)
        avg = pnl_sum / n
        print(f"\n{label}: {n} trades")
        print(f"  Win-rate:  {wins}/{n} = {wins/n*100:.1f}%  (losses {losses}, breakeven {n-wins-losses})")
        print(f"  Total P&L: ₹{pnl_sum:+,.0f}")
        print(f"  Avg P&L:   ₹{avg:+,.0f} per trade")

    summarize("WOULD-BE-BLOCKED (rule blocks these)", blocked)
    summarize("WOULD-PASS (rule lets these through)", passed)

    # The key delta: if rule had been live, we'd have kept the passed P&L and
    # avoided the blocked P&L.
    if blocked and passed:
        all_pnl     = sum(r["actual_pnl"] for r in blocked + passed)
        passed_pnl  = sum(r["actual_pnl"] for r in passed)
        blocked_pnl = sum(r["actual_pnl"] for r in blocked)
        print(f"\n── Net effect of rule ──")
        print(f"  Actual cumulative P&L (all trades):   ₹{all_pnl:+,.0f}")
        print(f"  If rule was live (passed only):       ₹{passed_pnl:+,.0f}")
        print(f"  Avoided P&L (blocked):                ₹{blocked_pnl:+,.0f}")
        print(f"  Delta (improvement = -blocked P&L):   ₹{-blocked_pnl:+,.0f}")
        if blocked_pnl < 0:
            print(f"  ✓ Rule would have improved P&L (blocked trades were net losers)")
        else:
            print(f"  ✗ Rule would have hurt P&L (blocked trades were net winners)")


if __name__ == "__main__":
    asyncio.run(main())
