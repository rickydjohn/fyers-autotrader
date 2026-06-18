"""
End-to-end smoke test of the equity-engine brain on SYNTHETIC daily data.

No Fyers, no network: a synthetic CandleProvider feeds deterministic OHLC series
(uptrend-with-pullback, fresh breakout, range, downtrend) through the real
features → signals → risk → screener pipeline. Verifies the whole chain runs and
behaves sanely:
  * at least one long candidate is produced,
  * the downtrend symbol is NOT picked (long-only),
  * every candidate has a coherent plan (stop < entry < target, R:R ≥ floor).

Run locally:
    cd equity-engine
    FYERS_CLIENT_ID=x FYERS_SECRET_KEY=x PYTHONPATH=. \
        python ../tests/equity/test_pipeline_synthetic.py
"""

import math
import os
import sys
from datetime import datetime, timedelta

import pytz

# Make equity-engine importable whether run via pytest or directly.
EQUITY_ENGINE = os.path.join(os.path.dirname(__file__), "..", "..", "equity-engine")
sys.path.insert(0, os.path.abspath(EQUITY_ENGINE))
os.environ.setdefault("FYERS_CLIENT_ID", "x")
os.environ.setdefault("FYERS_SECRET_KEY", "x")

from models import Bar, EquitySymbol, TrendRegime  # noqa: E402
from features import build_features  # noqa: E402
from screener import screen  # noqa: E402

IST = pytz.timezone("Asia/Kolkata")
_START = datetime(2025, 4, 1, tzinfo=IST)


def _series(base: float, trend: float, n: int = 360, pullback: float = 0.0) -> list[Bar]:
    bars = []
    for i in range(n):
        price = base + trend * i + 5.0 * math.sin(i / 9.0)
        if pullback and i >= n - 6:           # mild recent dip
            price -= pullback * (i - (n - 7))
        bars.append(
            Bar(
                timestamp=_START + timedelta(days=i),
                open=price - 1.0,
                high=price + 2.5,
                low=price - 2.5,
                close=price,
                volume=200_000,                # ~₹ tens of crore turnover → passes liquidity
            )
        )
    return bars


SYNTHETIC = {
    "NSE:UPTREND-EQ": _series(base=300.0, trend=0.8, pullback=4.0),   # trend pullback
    "NSE:BREAKOUT-EQ": _series(base=300.0, trend=1.2),               # pushing new highs
    "NSE:RANGE-EQ": _series(base=800.0, trend=0.0),                  # choppy, no trend
    "NSE:DOWNTREND-EQ": _series(base=900.0, trend=-0.6),             # falling — must be skipped
}


class SyntheticProvider:
    def daily_bars(self, symbol: str, limit: int = 250) -> list[Bar]:
        return SYNTHETIC.get(symbol, [])[-limit:]

    def quote(self, symbol: str):
        bars = SYNTHETIC.get(symbol)
        return {"ltp": bars[-1].close} if bars else None


UNIVERSE = [
    EquitySymbol(symbol=s, short_symbol=s.split(":")[1].replace("-EQ", ""), name=s)
    for s in SYNTHETIC
]


def test_pipeline_produces_sane_watchlist():
    candidates = screen(UNIVERSE, SyntheticProvider(), capital=1_000_000.0)
    picked = {c.symbol for c in candidates}

    assert candidates, "expected at least one long candidate"
    assert "NSE:DOWNTREND-EQ" not in picked, "downtrend must not be picked (long-only)"

    for c in candidates:
        p = c.plan
        assert p is not None
        assert p.stop < p.entry < p.target, f"{c.symbol}: incoherent plan {p}"
        assert p.risk_reward >= 1.5
        assert p.quantity > 0


def _regime_of(symbol: str) -> TrendRegime:
    return build_features(symbol, SYNTHETIC[symbol]).regime


if __name__ == "__main__":
    import logging
    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s: %(message)s")

    print("\nRegimes:")
    for s in SYNTHETIC:
        f = build_features(s, SYNTHETIC[s])
        print(f"  {s:<22} regime={f.regime.value:<9} rsi={f.rsi:>5.1f} "
              f"atr%={f.atr_pct:>4.1f} cpr={f.monthly_cpr.position} "
              f"turnover={f.avg_turnover_cr:.0f}cr")

    cands = screen(UNIVERSE, SyntheticProvider(), capital=1_000_000.0)
    print(f"\n=== {len(cands)} candidates ===")
    for c in cands:
        p = c.plan
        print(f"  {c.symbol:<22} {c.setup_type.value:<11} score={c.rank_score:.2f} "
              f"entry={p.entry} stop={p.stop} target={p.target} rr={p.risk_reward} qty={p.quantity}")
        for sig in c.signals:
            print(f"        · {sig.strategy}: {sig.rationale}")

    test_pipeline_produces_sane_watchlist()
    print("\n✓ smoke test passed")
