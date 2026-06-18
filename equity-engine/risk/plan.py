"""
Turn a Signal + Features into a sized, stop/target TradePlan.

Risk is anchored on ATR and the swing/positional bucket:
  * stop  = entry − (ATR × bucket multiplier)   (tighter for swing, wider for positional)
  * target = entry + (risk-per-share × target R)
  * size   = floor(capital × risk_per_trade% / risk-per-share), capped by max notional

So every trade risks a fixed fraction of capital regardless of the stock's price —
the discipline the options system never had (it sized by premium, not by risk).
Long-only in v1.
"""

import logging
from typing import Optional

from config import settings
from models import Features, SetupType, Side, Signal, TradePlan

logger = logging.getLogger(__name__)

TARGET_R = 2.0  # reward = 2× the risk distance


def build_plan(f: Features, signal: Signal, capital: Optional[float] = None) -> Optional[TradePlan]:
    if signal.side != Side.LONG:          # v1 held positions are long-only
        return None
    if f.atr <= 0:
        return None

    capital = capital if capital is not None else settings.initial_capital
    atr_mult = (
        settings.atr_stop_mult_positional
        if signal.setup_type == SetupType.POSITIONAL
        else settings.atr_stop_mult_swing
    )

    entry = signal.suggested_entry
    risk_per_share = atr_mult * f.atr
    stop = entry - risk_per_share
    if stop <= 0:
        return None

    target = entry + risk_per_share * TARGET_R
    risk_reward = (target - entry) / risk_per_share
    if risk_reward < settings.min_risk_reward:
        return None

    # Size by risk budget, then cap by max single-name notional.
    risk_budget = capital * settings.risk_per_trade_pct / 100.0
    qty = int(risk_budget // risk_per_share)
    max_notional = capital * settings.max_position_pct / 100.0
    if entry > 0:
        qty = min(qty, int(max_notional // entry))
    if qty <= 0:
        return None

    return TradePlan(
        symbol=f.symbol,
        side=Side.LONG,
        setup_type=signal.setup_type,
        entry=round(entry, 2),
        stop=round(stop, 2),
        target=round(target, 2),
        risk_reward=round(risk_reward, 2),
        quantity=qty,
        notional=round(qty * entry, 2),
        risk_amount=round(qty * risk_per_share, 2),
        rationale=signal.rationale,
    )
