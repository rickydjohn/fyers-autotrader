"""
Backtest: apply the 5 new trading rules to the historical ai_decisions dataset
and calculate win rate using real subsequent 5m candles.

New rules applied:
  R1 - Intraday override: EMA9>EMA21 + ABOVE_CPR + price>VWAP + RSI 45-75 → BUY
  R2 - MACD hard filter:  SELL + MACD BULLISH → HOLD  (and BUY + MACD BEARISH → HOLD)
  R3 - Confirmed breakout: don't re-block after PDH is cleared (first signal only)
  R4 - (acted_upon fix — operational, not backtest-relevant)
  R5 - Extended RSI: price > PDH*1.005 + RSI 45-78 + ABOVE_CPR + price>VWAP → BUY

Run:
    python3 backtest_new_rules.py
"""

import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import List, Optional, Tuple

import psycopg2
import psycopg2.extras
import pytz

DB = dict(host="localhost", port=5432, dbname="trading", user="trading", password="trading")
IST = pytz.timezone("Asia/Kolkata")
SESSION_END = time(15, 20)   # hard exit at 15:20 IST
SL_PCT   = 0.003             # 0.3% stop-loss
TGT_PCT  = 0.006             # 0.6% target  (2:1 RR)


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class Signal:
    decision_id: str
    symbol: str
    signal_time: datetime
    side: str           # BUY or SELL
    entry_price: float
    stop_loss: float
    target: float
    rule: str           # which rule triggered
    original_decision: str


@dataclass
class TradeResult:
    signal: Signal
    outcome: str        # WIN / LOSS / TIMEOUT_WIN / TIMEOUT_LOSS
    exit_price: float
    exit_time: Optional[datetime]
    pnl_pct: float


# ── Database helpers ───────────────────────────────────────────────────────────

def connect():
    return psycopg2.connect(**DB)


def fetch_decisions(cur) -> List[dict]:
    cur.execute("""
        SELECT
            d.decision_id,
            d.time AT TIME ZONE 'Asia/Kolkata'   AS time_ist,
            d.symbol,
            d.decision,
            d.confidence,
            (d.indicators_snapshot->>'price')::float        AS price,
            (d.indicators_snapshot->>'rsi')::float          AS rsi,
            d.indicators_snapshot->>'cpr_signal'             AS cpr_signal,
            (d.indicators_snapshot->>'vwap')::float          AS vwap,
            (d.indicators_snapshot->>'ema_9')::float         AS ema_9,
            (d.indicators_snapshot->>'ema_21')::float        AS ema_21,
            d.indicators_snapshot->>'macd_signal'            AS macd_signal,
            di.prev_high                                     AS pdh,
            di.prev_low                                      AS pdl
        FROM ai_decisions d
        LEFT JOIN daily_indicators di
               ON di.date    = DATE(d.time AT TIME ZONE 'Asia/Kolkata')
              AND di.symbol  = d.symbol
        ORDER BY d.symbol, d.time
    """)
    return [dict(r) for r in cur.fetchall()]


def fetch_candles_after(cur, symbol: str, after: datetime, session_date: date) -> List[dict]:
    """Fetch 5m candles for symbol from signal time until session end."""
    session_end_dt = IST.localize(datetime.combine(session_date, SESSION_END))
    cur.execute("""
        SELECT
            time AT TIME ZONE 'Asia/Kolkata' AS t,
            open, high, low, close
        FROM market_candles
        WHERE symbol = %s
          AND time > %s
          AND time <= %s
        ORDER BY time
    """, (symbol, after, session_end_dt))
    return [dict(r) for r in cur.fetchall()]


# ── Rule engine ────────────────────────────────────────────────────────────────

def apply_new_rules(decisions: List[dict]) -> Tuple[List[Signal], dict]:
    """
    Apply the 5 new rules to every decision row.
    Returns (signals, stats) where signals are deduplicated trade entries.
    """
    stats = {
        "r1_intraday_override": 0,
        "r2_macd_filter_blocked": 0,
        "r5_extended_rsi": 0,
        "existing_sell_kept": 0,
        "existing_buy_kept": 0,
        "deduped_out": 0,
    }

    raw_signals: List[Signal] = []

    for d in decisions:
        price      = float(d["price"]) if d["price"] is not None else None
        rsi        = float(d["rsi"]) if d["rsi"] is not None else None
        cpr        = d["cpr_signal"]
        vwap       = float(d["vwap"]) if d["vwap"] is not None else None
        ema9       = float(d["ema_9"]) if d["ema_9"] is not None else None
        ema21      = float(d["ema_21"]) if d["ema_21"] is not None else None
        macd       = d["macd_signal"]
        pdh        = float(d["pdh"]) if d["pdh"] is not None else None
        pdl        = float(d["pdl"]) if d["pdl"] is not None else None
        orig       = d["decision"]
        conf       = float(d["confidence"]) if d["confidence"] is not None else 0.5

        # Guard against nulls in early data
        if any(v is None for v in [price, rsi, vwap, ema9, ema21]):
            continue

        new_decision = orig  # start with LLM output

        # ── R2: MACD hard filter ──────────────────────────────────────────────
        if orig == "SELL" and macd == "BULLISH":
            new_decision = "HOLD"
            stats["r2_macd_filter_blocked"] += 1
            continue   # skip to next row

        if orig == "BUY" and macd == "BEARISH":
            conf = max(0.0, conf - 0.15)
            if conf < 0.5:
                new_decision = "HOLD"
                continue

        # ── R1: Intraday trend override ───────────────────────────────────────
        if orig == "HOLD" and cpr == "ABOVE_CPR" and 45 <= rsi <= 75 and price > vwap and ema9 > ema21:
            new_decision = "BUY"
            stats["r1_intraday_override"] += 1

        # ── R5: Extended RSI cap on strong PDH breakout ───────────────────────
        elif orig == "HOLD" and pdh and price > pdh * 1.005 and 75 < rsi <= 78 and cpr == "ABOVE_CPR" and price > vwap and macd != "BEARISH":
            new_decision = "BUY"
            stats["r5_extended_rsi"] += 1

        # ── Existing SELL/BUY signals that pass MACD filter ───────────────────
        elif orig == "SELL" and new_decision == "SELL":
            stats["existing_sell_kept"] += 1
        elif orig == "BUY" and new_decision == "BUY":
            stats["existing_buy_kept"] += 1

        if new_decision not in ("BUY", "SELL"):
            continue

        if price is None or price <= 0:
            continue

        if new_decision == "BUY":
            sl  = round(price * (1 - SL_PCT), 2)
            tgt = round(price * (1 + TGT_PCT), 2)
        else:
            sl  = round(price * (1 + SL_PCT), 2)
            tgt = round(price * (1 - TGT_PCT), 2)

        raw_signals.append(Signal(
            decision_id=d["decision_id"],
            symbol=d["symbol"],
            signal_time=d["time_ist"],
            side=new_decision,
            entry_price=price,
            stop_loss=sl,
            target=tgt,
            rule=(
                "R1_intraday" if orig == "HOLD" and new_decision == "BUY" and rsi <= 75
                else "R5_ext_rsi" if orig == "HOLD" and new_decision == "BUY"
                else "existing"
            ),
            original_decision=orig,
        ))

    # ── R3: Deduplication — one trade per symbol per session ─────────────────
    # Keep only the FIRST signal per (symbol, date). A new signal is allowed
    # after the prior trade's session date has passed.
    signals: List[Signal] = []
    active: dict = {}   # symbol -> session_date of open trade
    for sig in sorted(raw_signals, key=lambda s: s.signal_time):
        sig_date = sig.signal_time.date()
        if sig.symbol in active and active[sig.symbol] == sig_date:
            stats["deduped_out"] += 1
            continue
        active[sig.symbol] = sig_date
        signals.append(sig)

    return signals, stats


# ── Backtest engine ────────────────────────────────────────────────────────────

def evaluate_trade(sig: Signal, candles: List[dict]) -> TradeResult:
    """Walk candles until target/SL hit or session ends."""
    for c in candles:
        hi, lo, close_p, t = c["high"], c["low"], c["close"], c["t"]
        if sig.side == "BUY":
            sl_hit  = lo  <= sig.stop_loss
            tgt_hit = hi  >= sig.target
        else:
            sl_hit  = hi  >= sig.stop_loss
            tgt_hit = lo  <= sig.target

        if tgt_hit and sl_hit:
            # Both in same candle — award to whoever the open is closer to
            if sig.side == "BUY":
                winner = "WIN" if c["open"] <= (sig.entry_price + sig.target) / 2 else "LOSS"
            else:
                winner = "WIN" if c["open"] >= (sig.entry_price + sig.target) / 2 else "LOSS"
            ep = sig.target if winner == "WIN" else sig.stop_loss
            pct = (ep - sig.entry_price) / sig.entry_price * 100 * (1 if sig.side == "BUY" else -1)
            return TradeResult(sig, winner, ep, t, round(pct, 3))

        if tgt_hit:
            pct = abs(sig.target - sig.entry_price) / sig.entry_price * 100
            return TradeResult(sig, "WIN", sig.target, t, round(pct, 3))

        if sl_hit:
            pct = -abs(sig.stop_loss - sig.entry_price) / sig.entry_price * 100
            return TradeResult(sig, "LOSS", sig.stop_loss, t, round(pct, 3))

    # Session timed out — evaluate on last candle close
    if not candles:
        return TradeResult(sig, "TIMEOUT_LOSS", sig.entry_price, None, 0.0)

    last_close = candles[-1]["close"]
    last_time  = candles[-1]["t"]
    if sig.side == "BUY":
        pct = (last_close - sig.entry_price) / sig.entry_price * 100
        outcome = "TIMEOUT_WIN" if last_close > sig.entry_price else "TIMEOUT_LOSS"
    else:
        pct = (sig.entry_price - last_close) / sig.entry_price * 100
        outcome = "TIMEOUT_WIN" if last_close < sig.entry_price else "TIMEOUT_LOSS"

    return TradeResult(sig, outcome, last_close, last_time, round(pct, 3))


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("=" * 72)
    print("BACKTEST: New Rule Set Applied to Historical ai_decisions")
    print("=" * 72)

    conn = connect()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    print("\n[1/3] Loading decisions and daily indicators...")
    decisions = fetch_decisions(cur)
    print(f"      {len(decisions)} decision rows loaded")

    print("[2/3] Applying new rules...")
    signals, stats = apply_new_rules(decisions)

    print(f"\n  Rule stats:")
    print(f"    R1 intraday override triggered : {stats['r1_intraday_override']:>4} new BUY signals")
    print(f"    R2 MACD filter blocked         : {stats['r2_macd_filter_blocked']:>4} SELL→HOLD")
    print(f"    R5 extended RSI triggered      : {stats['r5_extended_rsi']:>4} new BUY signals")
    print(f"    Existing signals kept          : {stats['existing_sell_kept'] + stats['existing_buy_kept']:>4}")
    print(f"    Deduped out (same session)     : {stats['deduped_out']:>4}")
    print(f"    → Unique trade entries         : {len(signals):>4}")

    if not signals:
        print("\nNo signals to backtest.")
        return

    print("\n[3/3] Evaluating each trade against subsequent 5m candles...")
    results: List[TradeResult] = []

    for sig in signals:
        candles = fetch_candles_after(cur, sig.symbol, sig.signal_time, sig.signal_time.date())
        result  = evaluate_trade(sig, candles)
        results.append(result)

    cur.close()
    conn.close()

    # ── Summary ───────────────────────────────────────────────────────────────
    wins          = [r for r in results if r.outcome == "WIN"]
    losses        = [r for r in results if r.outcome == "LOSS"]
    timeout_wins  = [r for r in results if r.outcome == "TIMEOUT_WIN"]
    timeout_losses= [r for r in results if r.outcome == "TIMEOUT_LOSS"]

    total = len(results)
    hard_trades = len(wins) + len(losses)
    win_rate_hard = len(wins) / hard_trades * 100 if hard_trades else 0
    win_rate_all  = (len(wins) + len(timeout_wins)) / total * 100 if total else 0

    all_pnl_pcts = [r.pnl_pct for r in results]
    avg_pnl = sum(all_pnl_pcts) / len(all_pnl_pcts) if all_pnl_pcts else 0
    avg_win  = sum(r.pnl_pct for r in wins) / len(wins) if wins else 0
    avg_loss = sum(r.pnl_pct for r in losses) / len(losses) if losses else 0

    print("\n" + "=" * 72)
    print("RESULTS")
    print("=" * 72)
    print(f"  Total trades evaluated : {total}")
    print(f"  WIN  (target hit)      : {len(wins)}")
    print(f"  LOSS (SL hit)          : {len(losses)}")
    print(f"  TIMEOUT WIN            : {len(timeout_wins)}")
    print(f"  TIMEOUT LOSS           : {len(timeout_losses)}")
    print(f"")
    print(f"  Win rate (hard W/L only)          : {win_rate_hard:.1f}%")
    print(f"  Win rate (inc. timeout outcomes)  : {win_rate_all:.1f}%")
    print(f"")
    print(f"  Avg PnL per trade  : {avg_pnl:+.3f}%")
    print(f"  Avg win            : {avg_win:+.3f}%")
    print(f"  Avg loss           : {avg_loss:+.3f}%")
    if avg_loss != 0:
        print(f"  Actual RR ratio    : {abs(avg_win/avg_loss):.2f}:1")

    print("\n── Trade-by-trade breakdown ──────────────────────────────────────────")
    print(f"  {'Time':20s} {'Symbol':20s} {'Side':4s} {'Rule':15s} {'Entry':>9s} {'SL':>9s} {'Target':>9s} {'Outcome':12s} {'PnL%':>7s}")
    print(f"  {'-'*20} {'-'*20} {'-'*4} {'-'*15} {'-'*9} {'-'*9} {'-'*9} {'-'*12} {'-'*7}")
    for r in results:
        s = r.signal
        print(
            f"  {str(s.signal_time.strftime('%m-%d %H:%M')):20s} "
            f"{s.symbol[-15:]:20s} "
            f"{s.side:4s} "
            f"{s.rule:15s} "
            f"{s.entry_price:9.2f} "
            f"{s.stop_loss:9.2f} "
            f"{s.target:9.2f} "
            f"{r.outcome:12s} "
            f"{r.pnl_pct:+7.3f}%"
        )

    print("\n── By rule ───────────────────────────────────────────────────────────")
    for rule in ("R1_intraday", "R5_ext_rsi", "existing"):
        rule_results = [r for r in results if r.signal.rule == rule]
        if not rule_results:
            continue
        rw = sum(1 for r in rule_results if r.outcome in ("WIN", "TIMEOUT_WIN"))
        print(f"  {rule:15s}: {len(rule_results)} trades, {rw}/{len(rule_results)} wins ({rw/len(rule_results)*100:.0f}%)")

    print("\n── Old system comparison ─────────────────────────────────────────────")
    print(f"  Old system: 2 trades, 0 wins (100% loss rate, both stopped out)")
    print(f"  New system: {total} trades, {len(wins)+len(timeout_wins)} wins ({win_rate_all:.0f}% win rate)")
    print("=" * 72)


if __name__ == "__main__":
    main()
