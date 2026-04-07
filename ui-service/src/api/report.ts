import { apiClient } from './client'
import type { Trade } from '../types'

export interface ReasonBreakdown {
  count: number
  pnl: number
  wins: number
  losses: number
}

export interface MonthSummary {
  total_trades: number
  winners: number
  losers: number
  win_rate: number
  net_pnl: number
  gross_profit: number
  gross_loss: number
}

export interface MonthReport {
  month: string
  summary: MonthSummary
  by_exit_reason: Record<string, ReasonBreakdown>
  trades: Trade[]
}

export async function fetchMonthReport(month: string, tradingMode?: string): Promise<MonthReport> {
  const params: Record<string, string> = { month }
  if (tradingMode) params.trading_mode = tradingMode
  const res = await apiClient.get('/report/trades', { params })
  return res.data
}
