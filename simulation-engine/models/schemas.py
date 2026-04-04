from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import datetime


class Position(BaseModel):
    symbol: str          # underlying index symbol (position key)
    side: Literal["BUY", "SELL"]
    quantity: int
    avg_price: float     # option premium at entry
    entry_time: datetime
    stop_loss: float     # underlying price level
    target: float        # underlying price level
    decision_id: str
    option_symbol: Optional[str] = None    # e.g. NSE:NIFTY2640322200CE
    option_strike: Optional[int] = None
    option_type: Optional[str] = None      # CE or PE
    option_expiry: Optional[str] = None    # ISO date


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


class BudgetState(BaseModel):
    initial: float
    cash: float
    invested: float

    @property
    def total(self) -> float:
        return self.cash + self.invested

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
