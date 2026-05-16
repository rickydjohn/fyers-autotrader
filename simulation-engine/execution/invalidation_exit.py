"""
Tick-driven invalidation exit.

The premise: every LLM-initiated trade is built on a thesis stated in terms of
the index's relationship to a small set of technical levels — typically VWAP,
EMA21, and the CPR band. When the underlying crosses BACK through one of those
levels in the direction opposite the trade, the thesis is broken and the
fastest correct action is to flatten — independent of whether the option's
own SL has been hit yet.

This module exposes a single pure function, `check_invalidation_exit`, which
takes a position and the current underlying LTP and returns an exit reason
string if any captured invalidation level is crossed (None otherwise).

Why this fixes the 2026-05-14 BANKNIFTY53300PE trade specifically:

  Entry  11:15:29  SELL @ 53,341   thesis: below VWAP=53,589, EMA21=53,468, CPR-TC=53,520
  Price  11:18      ↑ crossed EMA21 (53,468)  → invalidation_ema_21 — would have exited here
  Price  11:20      ↑ crossed CPR-TC (53,520) → second invalidation, also would exit
  Price  11:22      hit option stop at -10%   → actual exit happened, -₹9,618

Catching the first cross at 11:18 instead of riding all the way down to the
−10% option stop at 11:22 saves roughly half the loss on that trade.

Captured `invalidation_levels` on the Position are set at open time (in
mock_broker / live_broker) from the decision's indicators_snapshot. Levels are
static for the lifetime of the trade — they reflect the conditions present
when the LLM formed its view; they do not move with price.
"""
from __future__ import annotations

from typing import Optional

from models.schemas import Position


# Tunable: how many basis points past the level price has to move before we
# treat it as a cross. 0 = strict cross. Small positive value avoids exiting
# on a single tick that grazes the level then immediately retraces.
INVALIDATION_BUFFER_PCT = 0.0    # set >0 (e.g. 0.0005 = 5bps) if false-cross noise becomes an issue


def check_invalidation_exit(pos: Position, underlying_ltp: float) -> Optional[str]:
    """Return an exit reason if the underlying has crossed back through any
    invalidation level in the direction opposite the trade.

    BUY/CE (bullish thesis) is invalidated when price falls below VWAP /
    EMA21 / CPR-BC (the supports the thesis relied on).

    SELL/PE (bearish thesis) is invalidated when price rises above VWAP /
    EMA21 / CPR-TC (the resistances the thesis relied on).

    Returns None if no invalidation has occurred, or if the position has no
    captured levels (e.g. an old position opened before this feature shipped).
    """
    levels = pos.invalidation_levels
    if not levels or underlying_ltp <= 0:
        return None

    buf = INVALIDATION_BUFFER_PCT

    if pos.side == "SELL":
        # Bearish thesis — price moving UP through resistance invalidates it.
        for name in ("vwap", "ema_21", "cpr_tc"):
            level = levels.get(name)
            if not level or level <= 0:
                continue
            if underlying_ltp > level * (1.0 + buf):
                return f"INVALIDATION_{name.upper()}"
    else:
        # Bullish thesis (BUY) — price moving DOWN through support invalidates it.
        for name in ("vwap", "ema_21", "cpr_bc"):
            level = levels.get(name)
            if not level or level <= 0:
                continue
            if underlying_ltp < level * (1.0 - buf):
                return f"INVALIDATION_{name.upper()}"

    return None
