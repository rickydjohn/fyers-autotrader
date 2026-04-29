"""
Backtest: Intraday Range Breakout rule applied to historical ai_decisions.

Rule:
  - Fetch the 40 completed 1m candles before each decision timestamp.
  - If (high - low) / midpoint < 0.40%  →  market was CONSOLIDATING.
  - If LTP > consolidation band high  →  BREAKOUT_HIGH
  - If LTP < consolidation band low   →  BREAKOUT_LOW
  - Confirmation filters (need ≥ 3 of 4):
      BREAKOUT_HIGH  +  price > VWAP  +  RSI 45-75  +  EMA9 > EMA21  +  MACD not BEARISH  →  BUY
      BREAKOUT_LOW   +  price < VWAP  +  RSI 25-55  +  EMA9 < EMA21  +  MACD not BULLISH  →  SELL
  - Deduplicate: first signal per (symbol, session date).
  - Outcome: subsequent 1m candles walk SL=0.3% / TGT=0.6% (2:1 RR).

Run:
    python3 backtest_range_breakout.py
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import List, Optional, Tuple

import psycopg2
import psycopg2.extras
import pytz

DB  = dict(host="localhost", port=5432, dbname="trading", user="trading", password="trading")
IST = pytz.timezone("Asia/Kolkata")

SESSION_END        = time(15, 20)
SL_PCT             = 0.003
TGT_PCT            = 0.006
CONSOL_LOOKBACK    = 40          # 1m candles  ≈  40 min consolidation window
CONSOL_THRESHOLD   = 0.40        # %  — range below this = sideways
BREAKOUT_BUFFER    = 0.0005      # price must clear band by 0.05% (not just touch)
CONFIRM_NEEDED     = 3           # need at least this many of 4 confirmation conditions


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class Signal:
    decision_id: str
    symbol: str
    signal_time: datetime
    side: str
    entry_price: float
    stop_loss: float
    target: float
    range_breakout: str      # BREAKOUT_HIGH | BREAKOUT_LOW
    consolidation_pct: float
    consol_high: float
    consol_low: float


@dataclass
class TradeResult:
    signal: Signal
    outcome: str          # WIN | LOSS | TIMEOUT_WIN | TIMEOUT_LOSS
    exit_price: float
    exit_time: Optional[datetime]
    pnl_pct: float


# ── DB helpers ─────────────────────────────────────────────────────────────────

def connect():
    return psycopg2.connect(**DB)


def fetch_decisions(cur) -> List[dict]:
    cur.execute("""
        SELECT
            d.decision_id,
            d.time AT TIME ZONE 'Asia/Kolkata'         AS time_ist,
            d.symbol,
            d.decision,
            (d.indicators_snapshot->>'price')::float   AS price,
            (d.indicators_snapshot->>'rsi')::float     AS rsi,
            (d.indicators_snapshot->>'vwap')::float    AS vwap,
            (d.indicators_snapshot->>'ema_9')::float   AS ema_9,
            (d.indicators_snapshot->>'ema_21')::float  AS ema_21,
            d.indicators_snapshot->>'macd_signal'      AS macd_signal
        FROM ai_decisions d
        ORDER BY d.symbol, d.time
    """)
    return [dict(r) for r in cur.fetchall()]


def fetch_prior_candles(cur, symbol: str, before: datetime, limit: int) -> List[dict]:
    """
    Fetch the most recent `limit` candles strictly before `before`, newest-first.
    Caller should skip index 0 (the bar that was current at decision time) and use
    the rest for the consolidation window — mirrors the live logic of
    candles[-(lookback+1):-1].
    """
    cur.execute("""
        SELECT
            time AT TIME ZONE 'Asia/Kolkata' AS t,
            high, low, close
        FROM market_candles
        WHERE symbol = %s
          AND time < %s
        ORDER BY time DESC
        LIMIT %s
    """, (symbol, before, limit))
    # Return newest-first so caller can easily skip [0]
    return [dict(r) for r in cur.fetchall()]


def fetch_candles_after(cur, symbol: str, after: datetime, session_date: date) -> List[dict]:
    end_dt = IST.localize(datetime.combine(session_date, SESSION_END))
    cur.execute("""
        SELECT
            time AT TIME ZONE 'Asia/Kolkata' AS t,
            open, high, low, close
        FROM market_candles
        WHERE symbol = %s
          AND time > %s
          AND time <= %s
        ORDER BY time
    """, (symbol, after, end_dt))
    return [dict(r) for r in cur.fetchall()]


# ── Consolidation + breakout logic ─────────────────────────────────────────────

def compute_consolidation(
    candles_newest_first: List[dict],
    current_price: float,
) -> Tuple[float, str, float, float]:
    """
    Returns (consolidation_pct, range_breakout, consol_high, consol_low).

    candles_newest_first[0]  = the bar that was 'current' at decision time (skip it).
    candles_newest_first[1:] = the prior consolidation window (mirrors live logic).
    """
    window = candles_newest_first[1:]   # skip the current bar
    if len(window) < 5:
        return 0.0, "NONE", current_price, current_price

    highs = [float(c["high"]) for c in window]
    lows  = [float(c["low"])  for c in window]
    w_high = max(highs)
    w_low  = min(lows)
    mid    = (w_high + w_low) / 2
    pct    = (w_high - w_low) / mid * 100 if mid > 0 else 0.0

    is_consolidating = pct < CONSOL_THRESHOLD

    if is_consolidating and current_price > w_high * (1 + BREAKOUT_BUFFER):
        breakout = "BREAKOUT_HIGH"
    elif is_consolidating and current_price < w_low * (1 - BREAKOUT_BUFFER):
        breakout = "BREAKOUT_LOW"
    else:
        breakout = "NONE"

    return round(pct, 3), breakout, round(w_high, 2), round(w_low, 2)


# ── Rule engine ────────────────────────────────────────────────────────────────

def apply_range_breakout_rule(decisions: List[dict], cur) -> Tuple[List[Signal], dict]:
    stats = {
        "decisions_checked":   0,
        "consolidating":       0,
        "breakout_high_raw":   0,
        "breakout_low_raw":    0,
        "buy_confirmed":       0,
        "sell_confirmed":      0,
        "confirmation_failed": 0,
        "null_indicators":     0,
        "deduped_out":         0,
    }

    raw_signals: List[Signal] = []

    for d in decisions:
        price  = d["price"]
        rsi    = d["rsi"]
        vwap   = d["vwap"]
        ema9   = d["ema_9"]
        ema21  = d["ema_21"]
        macd   = d["macd_signal"]

        if any(v is None for v in [price, rsi, vwap]):
            stats["null_indicators"] += 1
            continue

        price = float(price)
        rsi   = float(rsi)
        vwap  = float(vwap)
        ema9  = float(ema9) if ema9 is not None else None
        ema21 = float(ema21) if ema21 is not None else None

        stats["decisions_checked"] += 1

        prior = fetch_prior_candles(cur, d["symbol"], d["time_ist"], CONSOL_LOOKBACK + 1)
        consol_pct, breakout, c_high, c_low = compute_consolidation(prior, price)

        if consol_pct < CONSOL_THRESHOLD and consol_pct > 0:
            stats["consolidating"] += 1

        if breakout == "NONE":
            continue

        if breakout == "BREAKOUT_HIGH":
            stats["breakout_high_raw"] += 1
            # Confirmation: price > VWAP, RSI 45-75, EMA9 > EMA21, MACD not BEARISH
            confirmed = sum([
                price > vwap,
                45 <= rsi <= 75,
                (ema9 > ema21) if (ema9 and ema21) else False,
                macd != "BEARISH",
            ])
            if confirmed < CONFIRM_NEEDED:
                stats["confirmation_failed"] += 1
                continue
            stats["buy_confirmed"] += 1
            side = "BUY"
            sl   = round(price * (1 - SL_PCT), 2)
            tgt  = round(price * (1 + TGT_PCT), 2)

        else:  # BREAKOUT_LOW
            stats["breakout_low_raw"] += 1
            # Confirmation: price < VWAP, RSI 25-55, EMA9 < EMA21, MACD not BULLISH
            confirmed = sum([
                price < vwap,
                25 <= rsi <= 55,
                (ema9 < ema21) if (ema9 and ema21) else False,
                macd != "BULLISH",
            ])
            if confirmed < CONFIRM_NEEDED:
                stats["confirmation_failed"] += 1
                continue
            stats["sell_confirmed"] += 1
            side = "SELL"
            sl   = round(price * (1 + SL_PCT), 2)
            tgt  = round(price * (1 - TGT_PCT), 2)

        raw_signals.append(Signal(
            decision_id=d["decision_id"],
            symbol=d["symbol"],
            signal_time=d["time_ist"],
            side=side,
            entry_price=price,
            stop_loss=sl,
            target=tgt,
            range_breakout=breakout,
            consolidation_pct=consol_pct,
            consol_high=c_high,
            consol_low=c_low,
        ))

    # Dedup: first signal per (symbol, session date)
    signals: List[Signal] = []
    active: dict = {}
    for sig in sorted(raw_signals, key=lambda s: s.signal_time):
        sig_date = sig.signal_time.date()
        if sig.symbol in active and active[sig.symbol] == sig_date:
            stats["deduped_out"] += 1
            continue
        active[sig.symbol] = sig_date
        signals.append(sig)

    return signals, stats


# ── Trade evaluation ───────────────────────────────────────────────────────────

def evaluate_trade(sig: Signal, candles: List[dict]) -> TradeResult:
    for c in candles:
        hi, lo, t = float(c["high"]), float(c["low"]), c["t"]
        sl_hit  = (lo <= sig.stop_loss) if sig.side == "BUY" else (hi >= sig.stop_loss)
        tgt_hit = (hi >= sig.target)    if sig.side == "BUY" else (lo <= sig.target)

        if tgt_hit and sl_hit:
            winner = "WIN" if float(c["open"]) <= (sig.entry_price + sig.target) / 2 else "LOSS"
            ep  = sig.target if winner == "WIN" else sig.stop_loss
            pct = (ep - sig.entry_price) / sig.entry_price * 100 * (1 if sig.side == "BUY" else -1)
            return TradeResult(sig, winner, ep, t, round(pct, 3))

        if tgt_hit:
            pct = abs(sig.target - sig.entry_price) / sig.entry_price * 100
            return TradeResult(sig, "WIN", sig.target, t, round(pct, 3))

        if sl_hit:
            pct = -abs(sig.stop_loss - sig.entry_price) / sig.entry_price * 100
            return TradeResult(sig, "LOSS", sig.stop_loss, t, round(pct, 3))

    if not candles:
        return TradeResult(sig, "TIMEOUT_LOSS", sig.entry_price, None, 0.0)

    last_close = float(candles[-1]["close"])
    last_time  = candles[-1]["t"]
    if sig.side == "BUY":
        pct     = (last_close - sig.entry_price) / sig.entry_price * 100
        outcome = "TIMEOUT_WIN" if last_close > sig.entry_price else "TIMEOUT_LOSS"
    else:
        pct     = (sig.entry_price - last_close) / sig.entry_price * 100
        outcome = "TIMEOUT_WIN" if last_close < sig.entry_price else "TIMEOUT_LOSS"

    return TradeResult(sig, outcome, last_close, last_time, round(pct, 3))


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("=" * 76)
    print("BACKTEST: Intraday Range Breakout Rule")
    print(f"  Consolidation window : {CONSOL_LOOKBACK} × 1m candles (≈ {CONSOL_LOOKBACK} min)")
    print(f"  Consolidation thresh : < {CONSOL_THRESHOLD:.2f}% range")
    print(f"  Confirmations needed : {CONFIRM_NEEDED} of 4 (VWAP, RSI, EMA cross, MACD)")
    print(f"  SL / TGT             : {SL_PCT*100:.1f}% / {TGT_PCT*100:.1f}%  (RR 2:1)")
    print("=" * 76)

    conn = connect()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    print("\n[1/3] Loading decisions...")
    decisions = fetch_decisions(cur)
    print(f"      {len(decisions)} rows")

    print("[2/3] Applying range-breakout rule (fetches prior candles per decision)...")
    signals, stats = apply_range_breakout_rule(decisions, cur)

    print(f"\n  Funnel:")
    print(f"    Decisions checked          : {stats['decisions_checked']:>5}")
    print(f"    Periods in consolidation   : {stats['consolidating']:>5}")
    print(f"    Raw BREAKOUT_HIGH          : {stats['breakout_high_raw']:>5}")
    print(f"    Raw BREAKOUT_LOW           : {stats['breakout_low_raw']:>5}")
    print(f"    Confirmation failed (< {CONFIRM_NEEDED}) : {stats['confirmation_failed']:>5}")
    print(f"    BUY signals confirmed      : {stats['buy_confirmed']:>5}")
    print(f"    SELL signals confirmed     : {stats['sell_confirmed']:>5}")
    print(f"    Deduped (same session)     : {stats['deduped_out']:>5}")
    print(f"    → Unique trade entries     : {len(signals):>5}")

    if not signals:
        print("\nNo signals generated — dataset may be too small or no breakouts occurred.")
        return

    print("\n[3/3] Evaluating trades against subsequent 1m candles...")
    results: List[TradeResult] = []
    for sig in signals:
        candles = fetch_candles_after(cur, sig.symbol, sig.signal_time, sig.signal_time.date())
        results.append(evaluate_trade(sig, candles))

    cur.close()
    conn.close()

    # ── Summary ───────────────────────────────────────────────────────────────
    wins           = [r for r in results if r.outcome == "WIN"]
    losses         = [r for r in results if r.outcome == "LOSS"]
    timeout_wins   = [r for r in results if r.outcome == "TIMEOUT_WIN"]
    timeout_losses = [r for r in results if r.outcome == "TIMEOUT_LOSS"]

    total      = len(results)
    hard       = len(wins) + len(losses)
    wr_hard    = len(wins) / hard * 100 if hard else 0
    wr_all     = (len(wins) + len(timeout_wins)) / total * 100 if total else 0
    all_pnl    = [r.pnl_pct for r in results]
    avg_pnl    = sum(all_pnl) / len(all_pnl) if all_pnl else 0
    avg_win    = sum(r.pnl_pct for r in wins)   / len(wins)   if wins   else 0
    avg_loss   = sum(r.pnl_pct for r in losses) / len(losses) if losses else 0

    print("\n" + "=" * 76)
    print("RESULTS")
    print("=" * 76)
    print(f"  Total trades           : {total}")
    print(f"  WIN  (target hit)      : {len(wins)}")
    print(f"  LOSS (SL hit)          : {len(losses)}")
    print(f"  TIMEOUT WIN            : {len(timeout_wins)}")
    print(f"  TIMEOUT LOSS           : {len(timeout_losses)}")
    print()
    print(f"  Win rate (hard W/L)          : {wr_hard:.1f}%")
    print(f"  Win rate (inc. timeouts)     : {wr_all:.1f}%")
    print()
    print(f"  Avg PnL per trade  : {avg_pnl:+.3f}%")
    print(f"  Avg win            : {avg_win:+.3f}%")
    print(f"  Avg loss           : {avg_loss:+.3f}%")
    if avg_loss != 0:
        print(f"  Actual RR          : {abs(avg_win/avg_loss):.2f}:1")

    print("\n── Trade-by-trade ────────────────────────────────────────────────────")
    print(f"  {'Time':17s} {'Symbol':22s} {'Side':4s} {'Consol%':7s} {'Entry':>9s} {'SL':>9s} {'TGT':>9s} {'Outcome':12s} {'PnL%':>7s}")
    print(f"  {'-'*17} {'-'*22} {'-'*4} {'-'*7} {'-'*9} {'-'*9} {'-'*9} {'-'*12} {'-'*7}")
    for r in results:
        s = r.signal
        print(
            f"  {s.signal_time.strftime('%m-%d %H:%M'):17s} "
            f"{s.symbol[-20:]:22s} "
            f"{s.side:4s} "
            f"{s.consolidation_pct:6.2f}% "
            f"{s.entry_price:9.2f} "
            f"{s.stop_loss:9.2f} "
            f"{s.target:9.2f} "
            f"{r.outcome:12s} "
            f"{r.pnl_pct:+7.3f}%"
        )

    print("\n── Breakout direction breakdown ──────────────────────────────────────")
    for direction in ("BREAKOUT_HIGH", "BREAKOUT_LOW"):
        grp = [r for r in results if r.signal.range_breakout == direction]
        if not grp:
            continue
        gw = sum(1 for r in grp if r.outcome in ("WIN", "TIMEOUT_WIN"))
        side_label = "BUY (Call)" if direction == "BREAKOUT_HIGH" else "SELL (Put)"
        print(f"  {direction} → {side_label:12s}: {len(grp):2d} trades, {gw}/{len(grp)} wins ({gw/len(grp)*100:.0f}%)")

    print("\n── Consolidation tightness of winning vs losing trades ───────────────")
    if wins:
        avg_w_pct = sum(r.signal.consolidation_pct for r in wins) / len(wins)
        print(f"  Avg consolidation% (winners): {avg_w_pct:.3f}%")
    if losses:
        avg_l_pct = sum(r.signal.consolidation_pct for r in losses) / len(losses)
        print(f"  Avg consolidation% (losers) : {avg_l_pct:.3f}%")

    print("=" * 76)


if __name__ == "__main__":
    main()
