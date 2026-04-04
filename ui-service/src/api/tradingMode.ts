import { apiClient } from './client'
import type { FundsData, SimulationBudgetData, TradingMode } from '../types'

export async function fetchTradingMode(): Promise<{ mode: TradingMode }> {
  const r = await apiClient.get('/trading-mode')
  return r.data
}

export async function updateTradingMode(mode: TradingMode): Promise<{ mode: TradingMode }> {
  const r = await apiClient.post('/trading-mode', null, { params: { mode } })
  return r.data
}

export async function fetchFunds(): Promise<FundsData> {
  const r = await apiClient.get('/funds')
  return r.data.funds ?? {}
}

export async function fetchSimulationBudget(): Promise<SimulationBudgetData> {
  const r = await apiClient.get('/simulation-budget')
  return {
    initial: r.data.initial ?? 0,
    current: r.data.current ?? 0,
    cash: r.data.cash ?? 0,
    invested: r.data.invested ?? 0,
    utilization_pct: r.data.utilization_pct ?? 0,
  }
}
