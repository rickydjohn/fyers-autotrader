from pydantic import BaseModel, Field
from typing import Optional, List, Literal
from datetime import datetime


class OHLCBar(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


class CPRResult(BaseModel):
    pivot: float
    bc: float   # Bottom Central
    tc: float   # Top Central
    width_pct: float
    is_narrow: bool
    day_type: str = "WIDE"  # NARROW | MODERATE | WIDE (ATR-normalised classification)


class PivotLevels(BaseModel):
    pivot: float
    r1: float
    r2: float
    r3: float
    r4: float
    r5: float
    s1: float
    s2: float
    s3: float
    s4: float
    s5: float


class TechnicalIndicators(BaseModel):
    cpr: CPRResult
    pivots: PivotLevels
    rsi: float
    vwap: float
    macd: float
    macd_signal: float
    macd_histogram: float
    ema_9: float
    ema_21: float
    cpr_signal: Literal["ABOVE_CPR", "BELOW_CPR", "INSIDE_CPR"]
    nearest_resistance: float
    nearest_resistance_label: str
    nearest_support: float
    nearest_support_label: str
    prev_day_high: float = 0.0
    prev_day_low: float = 0.0
    # Intraday range breakout indicators
    day_high: float = 0.0          # highest point reached today (intraday)
    day_low: float = 0.0           # lowest point reached today (intraday)
    consolidation_pct: float = 0.0 # range% of last 8 candles; < 0.40 = sideways
    range_breakout: Literal["BREAKOUT_HIGH", "BREAKOUT_LOW", "NONE"] = "NONE"
    pdh_pivot_confluence: bool = False  # PDH within 0.2% of daily Pivot


class NewsItem(BaseModel):
    title: str
    summary: str
    source: str
    published_at: datetime
    sentiment_score: float = 0.0  # -1.0 to 1.0


class NewsSentiment(BaseModel):
    items: List[NewsItem]
    aggregate_score: float
    label: Literal["BEARISH", "NEUTRAL", "BULLISH"]


class MarketSnapshot(BaseModel):
    symbol: str
    ltp: float
    change: float
    change_pct: float
    volume: int
    timestamp: datetime
    candles: List[OHLCBar]
    indicators: TechnicalIndicators
    news: Optional[NewsSentiment] = None


class LLMDecision(BaseModel):
    decision_id: str
    symbol: str
    timestamp: datetime
    decision: Literal["BUY", "SELL", "HOLD"]
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    stop_loss: float
    target: float
    risk_reward: float
    indicators_snapshot: dict
    acted_upon: bool = False
    trade_id: Optional[str] = None
    # Options fields (populated for BUY/SELL decisions)
    option_symbol: Optional[str] = None     # e.g. NSE:NIFTY2640322200CE
    option_strike: Optional[int] = None     # e.g. 22200
    option_type: Optional[str] = None       # CE or PE
    option_expiry: Optional[str] = None     # ISO date e.g. 2026-04-03
    option_price: Optional[float] = None    # option LTP at decision time
    option_lot_size: Optional[int] = None   # derived from Fyers market depth
