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


def build_invalidation_levels(
    decision: str, current_price: float, ind_dict: dict
) -> Optional[dict]:
    """Snapshot the index levels the LLM's thesis was built on, filtered to
    only those that are ADVERSE to the trade direction at entry.

    A level is "adverse" if crossing it ends the thesis:
      - SELL (bearish): adverse = level ABOVE current price (price crossing
        UP through it invalidates).
      - BUY (bullish): adverse = level BELOW current price (price crossing
        DOWN through it invalidates).

    Levels on the trade-favorable side are already past us in the trade
    direction; including them would cause an instant false-positive exit
    when `check_invalidation_exit` runs on the first tick.

    All levels are treated uniformly. CPR-TC and CPR-BC are just two more
    levels — filtered by position relative to current price, not by which
    "side" of CPR they nominally represent.

    Returns None when no level is adverse (the trade was opened with no
    nearby barrier in the unfavorable direction — relies on premium SL).
    """
    def _f(key: str) -> Optional[float]:
        v = ind_dict.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    candidates = {
        "vwap":   _f("vwap"),
        "ema_21": _f("ema_21"),
        "cpr_tc": _f("cpr_tc"),
        "cpr_bc": _f("cpr_bc"),
    }
    if decision == "SELL":
        return {
            k: v for k, v in candidates.items()
            if v is not None and v > current_price
        } or None
    if decision == "BUY":
        return {
            k: v for k, v in candidates.items()
            if v is not None and v < current_price
        } or None
    return None


# Tunable: how many basis points past the level price has to move before we
# treat it as a cross. 0 = strict cross. Small positive value avoids exiting
# on a single tick that grazes the level then immediately retraces.
INVALIDATION_BUFFER_PCT = 0.0    # set >0 (e.g. 0.0005 = 5bps) if false-cross noise becomes an issue


def check_invalidation_exit(pos: Position, underlying_ltp: float) -> Optional[str]:
    """Return an exit reason if the underlying has crossed back through any
    captured invalidation level in the direction opposite the trade.

    Captured levels are filtered at entry time so that only ADVERSE levels
    are stored (see `_handle_decision` in main.py). That means:
      - SELL positions hold levels that were ABOVE entry price.
        Price crossing UP through any of them invalidates the bearish thesis.
      - BUY positions hold levels that were BELOW entry price.
        Price crossing DOWN through any of them invalidates the bullish thesis.

    All captured levels are treated uniformly — CPR's TC and BC are just two
    of the levels, no different from VWAP or EMA21. We iterate whatever the
    capture step decided was adverse.

    Returns None if no invalidation has occurred or if the position has no
    captured levels (some entries leave the dict empty when no nearby level
    is adverse — those positions rely on premium SL alone).
    """
    levels = pos.invalidation_levels
    if not levels or underlying_ltp <= 0:
        return None

    buf = INVALIDATION_BUFFER_PCT

    if pos.side == "SELL":
        # Captured levels were above price at entry; crossing UP invalidates.
        for name, level in levels.items():
            if not level or level <= 0:
                continue
            if underlying_ltp > level * (1.0 + buf):
                return f"INVALIDATION_{name.upper()}"
    else:
        # Captured levels were below price at entry; crossing DOWN invalidates.
        for name, level in levels.items():
            if not level or level <= 0:
                continue
            if underlying_ltp < level * (1.0 - buf):
                return f"INVALIDATION_{name.upper()}"

    return None
