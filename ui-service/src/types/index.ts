export interface OHLCBar {
  timestamp: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface CPR {
  bc: number
  tc: number
  pivot: number
  width_pct: number
  is_narrow: boolean
}

export interface Pivots {
  pivot: number
  r1: number; r2: number; r3: number
  s1: number; s2: number; s3: number
}

export interface Indicators {
  cpr: CPR
  pivots: Pivots
  rsi: number
  vwap: number
  macd: number
  macd_signal: number
  macd_histogram: number
  ema_9: number
  ema_21: number
  cpr_signal: 'ABOVE_CPR' | 'BELOW_CPR' | 'INSIDE_CPR'
  nearest_resistance: number
  nearest_resistance_label: string
  nearest_support: number
  nearest_support_label: string
}

export interface MarketData {
  symbol: string
  ltp: number
  change: number
  change_pct: number
  volume: number
  timestamp: string
  candles: OHLCBar[]
  indicators: Indicators
}

export interface Trade {
  trade_id: string
  symbol: string
  side: 'BUY' | 'SELL'
  quantity: number
  entry_price: number
  exit_price?: number
  entry_time: string
  exit_time?: string
  pnl?: number
  pnl_pct?: number
  commission: number
  slippage: number
  status: 'OPEN' | 'CLOSED' | 'STOPPED'
  decision_id: string
  reasoning: string
  option_symbol?: string
  option_strike?: number
  option_type?: string   // CE or PE
  option_expiry?: string
}

export interface Position {
  symbol: string
  side: 'BUY' | 'SELL'
  quantity: number
  avg_price: number
  ltp?: number
  unrealized_pnl?: number
  unrealized_pnl_pct?: number
  stop_loss: number
  target: number
  entry_time: string
  option_symbol?: string
  option_strike?: number
  option_type?: string
  option_expiry?: string
}

export interface PnLData {
  period: string
  realized_pnl: number
  unrealized_pnl: number
  total_pnl: number
  total_pnl_pct: number
  budget: {
    initial: number
    current: number
    cash: number
    invested: number
    utilization_pct: number
  }
  win_rate: number
  avg_win: number
  avg_loss: number
  total_trades: number
  timeline: Array<{ timestamp: string; cumulative_pnl: number }>
}

export interface Decision {
  decision_id: string
  symbol: string
  timestamp: string
  decision: 'BUY' | 'SELL' | 'HOLD'
  confidence: number
  reasoning: string
  stop_loss: number
  target: number
  risk_reward: number
  indicators_snapshot: {
    price: number
    cpr_signal: string
    rsi: number
    sentiment_score: number
    macd_signal: string
  }
  acted_upon: boolean
  option_symbol?: string
  option_strike?: number
  option_type?: string
  option_expiry?: string
  option_price?: number
}

export type Timeframe = '1m' | '5m' | '15m' | '1h' | 'daily'

export type TradingMode = 'simulation' | 'live'

export interface FundsData {
  available_balance?: number
  net_available?: number
  total_balance?: number
  used_amount?: number
  [key: string]: number | undefined
}

export interface SimulationBudgetData {
  initial: number
  current: number
  cash: number
  invested: number
  utilization_pct: number
}

export interface HistoricalCandle {
  time: string
  open: number
  high: number
  low: number
  close: number
  volume: number
  vwap?: number
  rsi?: number
}

export interface MultiTimeframeTrend {
  '15m': string
  '1h': string
  daily: string
}

export interface ContextSnapshot {
  generated_at: string
  symbol: string
  previous_day: {
    high: number
    low: number
    close: number
    date: string
  }
  today_cpr: {
    pivot: number
    bc: number
    tc: number
    r1: number
    r2: number
    s1: number
    s2: number
    cpr_width_pct: number
    cpr_type: string
  }
  key_levels: {
    resistance_zones: number[]
    support_zones: number[]
  }
  multi_timeframe_trend: MultiTimeframeTrend
  intraday_range: { high: number; low: number }
  volatility: { '15m_atr_pct': number; daily_atr_pct: number }
  news_sentiment: {
    count: number
    avg_score: number
    label: string
    headlines: Array<{ title: string; source: string; score: number }>
  }
  recent_trade_outcomes: {
    count: number
    recent?: Array<{ decision: string; confidence: number; reasoning: string }>
  }
}
