"""
v1 signal strategies — long-only, deterministic.

These are intentionally simple, transparent heuristics: the value is the modular
pipeline, not magic thresholds (those get tuned by the backtest harness). Each
returns a Signal with a 0..1 conviction score and the raw ingredients behind it, so
every pick is explainable.

  TrendPullback     — buy a healthy uptrend that has pulled back to support/EMA
                      (RSI cooled but not broken). Classic swing entry.
  MonthlyCprReclaim — price holding ABOVE the monthly CPR (the user's level frame);
                      a narrow monthly CPR implies a trending month → stronger.
  Breakout52w       — price pushing into 52-week-high territory with momentum.
"""

from typing import Optional

from models import Features, Side, Signal, SetupType, TrendRegime
from signals.base import clamp01
from signals.classifier import classify


class TrendPullback:
    name = "trend_pullback"

    def evaluate(self, f: Features) -> Optional[Signal]:
        if f.regime != TrendRegime.UPTREND:
            return None
        if not (38.0 <= f.rsi <= 58.0):          # pulled back, not yet weak/overbought
            return None

        anchor = f.ema_20 if f.ema_20 else f.ltp
        dist_to_ema = abs(f.ltp - anchor) / f.ltp * 100.0
        near_support = 0.0 <= f.dist_to_support_pct <= 4.0
        if dist_to_ema > 4.0 and not near_support:
            return None

        prox_score = clamp01(1.0 - dist_to_ema / 4.0)
        rsi_score = clamp01(1.0 - abs(f.rsi - 48.0) / 20.0)   # sweet spot ~48
        trend_score = clamp01(((f.ltp - f.ema_50) / f.ema_50 * 100.0) / 5.0) if f.ema_50 else 0.3
        score = clamp01(0.40 * prox_score + 0.30 * rsi_score + 0.30 * trend_score)

        return Signal(
            symbol=f.symbol,
            strategy=self.name,
            setup_type=classify(f),
            side=Side.LONG,
            score=round(score, 3),
            rationale=(
                f"Uptrend pullback: RSI {f.rsi:.0f}, {dist_to_ema:.1f}% from 20-EMA, "
                f"{f.dist_to_support_pct:.1f}% above support {f.nearest_support}"
            ),
            suggested_entry=f.ltp,
            ingredients={"rsi": f.rsi, "dist_to_ema_pct": round(dist_to_ema, 2),
                         "dist_to_support_pct": f.dist_to_support_pct},
        )


class MonthlyCprReclaim:
    name = "monthly_cpr_break"

    def evaluate(self, f: Features) -> Optional[Signal]:
        cpr = f.monthly_cpr
        if cpr.position != "ABOVE_CPR" or f.regime == TrendRegime.DOWNTREND:
            return None

        narrow_score = clamp01(1.0 - cpr.width_pct / 2.0)     # narrow monthly CPR → trending month
        above_tc_pct = (f.ltp - cpr.tc) / f.ltp * 100.0 if f.ltp else 0.0
        prox_score = clamp01(1.0 - above_tc_pct / 8.0)        # prefer not over-extended above CPR
        mom_score = clamp01(0.5 * (1.0 if f.macd_histogram > 0 else 0.0)
                            + 0.5 * (1.0 if f.rsi > 52.0 else 0.0))
        score = clamp01(0.40 * narrow_score + 0.30 * prox_score + 0.30 * mom_score)

        return Signal(
            symbol=f.symbol,
            strategy=self.name,
            setup_type=classify(f),
            side=Side.LONG,
            score=round(score, 3),
            rationale=(
                f"Above monthly CPR (pivot {cpr.pivot}, width {cpr.width_pct:.2f}%), "
                f"{above_tc_pct:.1f}% over TC, RSI {f.rsi:.0f}"
            ),
            suggested_entry=f.ltp,
            ingredients={"cpr_pivot": cpr.pivot, "cpr_width_pct": cpr.width_pct,
                         "above_tc_pct": round(above_tc_pct, 2)},
        )


class Breakout52w:
    name = "breakout_52w"

    def evaluate(self, f: Features) -> Optional[Signal]:
        if f.regime == TrendRegime.DOWNTREND or f.rsi < 52.0:
            return None
        if f.pct_from_52w_high < -6.0:          # too far below the high to call it a breakout
            return None

        prox_score = clamp01(1.0 - abs(f.pct_from_52w_high) / 6.0)
        mom_score = clamp01(0.5 * (1.0 if f.macd_histogram > 0 else 0.0)
                            + 0.5 * clamp01((f.rsi - 50.0) / 20.0))
        score = clamp01(0.55 * prox_score + 0.45 * mom_score)
        setup = SetupType.POSITIONAL if f.regime == TrendRegime.UPTREND else SetupType.SWING

        return Signal(
            symbol=f.symbol,
            strategy=self.name,
            setup_type=setup,
            side=Side.LONG,
            score=round(score, 3),
            rationale=(
                f"52w-high breakout: {f.pct_from_52w_high:+.1f}% from high, "
                f"RSI {f.rsi:.0f}, MACD hist {f.macd_histogram:+.2f}"
            ),
            suggested_entry=f.ltp,
            ingredients={"pct_from_52w_high": f.pct_from_52w_high, "rsi": f.rsi,
                         "macd_hist": f.macd_histogram},
        )
