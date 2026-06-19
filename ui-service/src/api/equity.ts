import { apiClient } from './client'

export interface EquityRec {
  action?: string
  conviction?: string
  reasons?: string
  stop?: number
  target?: number
  key_resistance?: number
  entry_zone?: number[]
}

export interface EquityCandidate {
  symbol: string
  name: string
  ltp: number
  regime: string
  momentum_12m_pct: number
  pct_from_52w_high?: number
  resistances: number[]
  supports: number[]
  recommendation?: EquityRec
}

export interface EquityHolding {
  symbol: string
  name: string
  recommendation?: EquityRec
}

export interface EquityPosition {
  symbol: string
  name: string
  source: string // ACTUAL | PAPER
  qty: number
  avg_price: number
  ltp: number
  pnl: number
  pnl_pct: number
}

export interface EquityReport {
  generated_at?: string
  holdings: EquityHolding[]
  candidates: EquityCandidate[]
}

export const fetchEquityPositions = () =>
  apiClient.get('/equity/positions').then((r) => r.data as { positions: EquityPosition[]; mode: string })

export const fetchEquityAnalysisCached = () =>
  apiClient.get('/equity/analysis/cached').then((r) => r.data as { report: EquityReport | null })

export const refreshEquityAnalysis = (candidates = 8) =>
  apiClient.post('/equity/analysis/refresh', null, { params: { candidates } }).then((r) => r.data)

export const getEquityMode = () =>
  apiClient.get('/equity/mode').then((r) => r.data as { mode: string })

export const setEquityMode = (mode: string) =>
  apiClient.post('/equity/mode', null, { params: { mode } }).then((r) => r.data as { mode: string })

export const equityTrade = (symbol: string, side: string, qty: number, confirm = false) =>
  apiClient.post('/equity/trade', null, { params: { symbol, side, qty, confirm } }).then((r) => r.data)
