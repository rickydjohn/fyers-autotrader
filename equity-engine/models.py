"""
equity-engine domain contracts.

These are the typed objects that flow between pipeline stages. Each stage consumes
one and produces the next, so the contracts ARE the architecture:

    EquitySymbol  →  (data)     →  Bar[]
    Bar[]         →  (features) →  Features
    Features      →  (signals)  →  Signal[]
    Signal[]      →  (screener) →  Candidate            (ranked)
    Candidate     →  (risk)     →  TradePlan            (sized, stop/target)
    TradePlan     →  (execution)→  Position / EquityTrade

Keeping these small and explicit is what makes the modules swappable: a new strategy
only has to emit `Signal`s; a new exit rule only reads a `Position`.
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ── Enums ───────────────────────────────────────────────────────────────────--
class Timeframe(str, Enum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"


class SetupType(str, Enum):
    """Adaptive bucket. Decides which exit/risk parameters apply."""
    SWING = "SWING"            # days–weeks; tighter ATR stop, faster targets
    POSITIONAL = "POSITIONAL"  # weeks–months; wider stop, trend-ride exits


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"            # v1 held positions are LONG-only (cash); SHORT reserved


class TrendRegime(str, Enum):
    UPTREND = "UPTREND"
    DOWNTREND = "DOWNTREND"
    RANGE = "RANGE"


# ── Universe ─────────────────────────────────────────────────────────────────
class EquitySymbol(BaseModel):
    """One tradable NSE cash equity, as enumerated from the Fyers symbol master."""
    symbol: str            # Fyers ticker, e.g. "NSE:SBIN-EQ"
    short_symbol: str      # bare ticker, e.g. "SBIN"
    name: str              # company name
    isin: str = ""
    lot_size: int = 1


# ── Market data ──────────────────────────────────────────────────────────────
class Bar(BaseModel):
    """A single OHLCV bar at some timeframe. Mirrors core-engine's OHLCBar so the
    Fyers data wrapper's output drops straight in."""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


# ── Features (computed per symbol, per scan) ─────────────────────────────────-
class MonthlyCPR(BaseModel):
    """Monthly Central Pivot Range — the user's requested level framework."""
    pivot: float
    bc: float              # bottom central
    tc: float              # top central
    width_pct: float
    position: str          # ABOVE_CPR | INSIDE_CPR | BELOW_CPR (price vs CPR)


class Features(BaseModel):
    """Everything a signal strategy needs about one symbol at scan time. Computed
    from daily bars + monthly aggregation; no options/Greeks anywhere."""
    symbol: str
    ltp: float
    asof: datetime

    # Trend / momentum (daily)
    regime: TrendRegime
    rsi: float
    macd: float
    macd_signal: float
    macd_histogram: float
    ema_20: float
    ema_50: float
    ema_200: float
    atr: float                       # absolute, in price units
    atr_pct: float                   # atr / ltp * 100

    # Liquidity
    avg_turnover_cr: float = 0.0     # 20-day avg traded value, ₹ crore
    avg_volume: float = 0.0

    # Levels
    monthly_cpr: MonthlyCPR
    nearest_support: float = 0.0
    nearest_resistance: float = 0.0
    dist_to_support_pct: float = 0.0
    dist_to_resistance_pct: float = 0.0

    # Position within recent range
    pct_from_52w_high: float = 0.0
    pct_from_52w_low: float = 0.0


# ── Signals ──────────────────────────────────────────────────────────────────
class Signal(BaseModel):
    """One strategy's verdict on one symbol. Score is 0..1 (strength/conviction).
    A symbol can collect several signals from different strategies; the screener
    aggregates them."""
    symbol: str
    strategy: str                    # e.g. "trend_pullback", "monthly_cpr_break"
    setup_type: SetupType
    side: Side = Side.LONG
    score: float = Field(ge=0.0, le=1.0)
    rationale: str
    suggested_entry: float           # trigger / reference entry price
    ingredients: dict = Field(default_factory=dict)  # the raw values behind the score


# ── Ranked candidate ─────────────────────────────────────────────────────────
class Candidate(BaseModel):
    """A symbol that cleared the screen, with its aggregated score and the plan."""
    symbol: str
    rank_score: float
    setup_type: SetupType
    side: Side
    signals: list[Signal]
    plan: Optional["TradePlan"] = None


# ── Trade plan (sized, with stop/target) ─────────────────────────────────────
class TradePlan(BaseModel):
    symbol: str
    side: Side
    setup_type: SetupType
    entry: float
    stop: float
    target: float
    risk_reward: float
    quantity: int
    notional: float
    risk_amount: float               # ₹ at risk if stopped (entry-stop) * qty
    rationale: str


# ── Execution / portfolio ─────────────────────────────────────────────────────
class Position(BaseModel):
    symbol: str
    side: Side
    setup_type: SetupType
    quantity: int
    entry_price: float
    entry_time: datetime
    stop: float
    target: float
    highest_close: float = 0.0       # for trailing (positional ride)
    plan_rationale: str = ""


class EquityTrade(BaseModel):
    trade_id: str
    symbol: str
    side: Side
    setup_type: SetupType
    quantity: int
    entry_price: float
    entry_time: datetime
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    exit_reason: Optional[str] = None
    pnl: float = 0.0
    pnl_pct: float = 0.0
    commission: float = 0.0
    trading_mode: str = "paper"
    status: str = "OPEN"             # OPEN | CLOSED


Candidate.model_rebuild()
