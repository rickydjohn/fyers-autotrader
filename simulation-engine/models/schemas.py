from pydantic import BaseModel, Field, computed_field
from typing import Optional, Literal
from datetime import datetime


class Position(BaseModel):
    symbol: str          # underlying index symbol (position key)
    side: Literal["BUY", "SELL"]
    quantity: int
    avg_price: float     # option premium at entry
    entry_time: datetime
    stop_loss: float     # option premium level for option trades; underlying price for equity
    target: float        # option premium level for option trades; underlying price for equity
    decision_id: str
    option_symbol: Optional[str] = None    # e.g. NSE:NIFTY2640322200CE
    option_strike: Optional[int] = None
    option_type: Optional[str] = None      # CE or PE
    option_expiry: Optional[str] = None    # ISO date
    # Exit-rules support
    entry_option_price: float = 0.0        # option premium at open (for drawdown %)
    peak_option_price: float = 0.0         # highest observed option LTP since entry (for trailing)
    entry_iv: float = 0.0                  # implied volatility at entry (for IV crush detection)
    milestone_count: int = 0               # trail milestone counter (0 = pre-trail; increments at each +10%)
    day_type: Optional[str] = None         # "RANGING" or "TRENDING" — set at entry from CPR width


class Trade(BaseModel):
    trade_id: str
    symbol: str          # option symbol when trading options, else underlying
    side: Literal["BUY", "SELL"]
    quantity: int
    entry_price: float
    exit_price: Optional[float] = None
    entry_time: datetime
    exit_time: Optional[datetime] = None
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
    commission: float = 0.0
    slippage: float = 0.0
    status: Literal["OPEN", "CLOSED", "STOPPED"] = "OPEN"
    decision_id: str
    reasoning: str = ""
    option_symbol: Optional[str] = None
    option_strike: Optional[int] = None
    option_type: Optional[str] = None
    option_expiry: Optional[str] = None
    exit_reason: Optional[str] = None      # detailed exit cause (e.g. PREMIUM_DECAY, TRAIL_STOP)
    broker_order_id: Optional[str] = None  # Fyers order ID for live trades (entry order)


class BudgetState(BaseModel):
    initial: float
    cash: float
    invested: float

    @computed_field
    @property
    def current(self) -> float:
        """Total current portfolio value (cash + invested). Used by the UI."""
        return round(self.cash + self.invested, 2)

    @computed_field
    @property
    def utilization_pct(self) -> float:
        return round(self.invested / self.initial * 100, 2) if self.initial > 0 else 0.0


class PnLSnapshot(BaseModel):
    timestamp: datetime
    realized_pnl: float
    unrealized_pnl: float
    total_pnl: float
    total_pnl_pct: float
    win_count: int
    loss_count: int
    total_trades: int
