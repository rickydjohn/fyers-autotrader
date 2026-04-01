from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import datetime


class Position(BaseModel):
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: int
    avg_price: float
    entry_time: datetime
    stop_loss: float
    target: float
    decision_id: str


class Trade(BaseModel):
    trade_id: str
    symbol: str
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
