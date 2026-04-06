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
  const initial: number = r.data.initial ?? 0
  const cash: number = r.data.cash ?? 0
  const invested: number = r.data.invested ?? 0
  // Backend computes current via @computed_field; fall back to cash+invested if absent
  const current: number = r.data.current ?? (cash + invested)
  const utilization_pct: number =
    r.data.utilization_pct ?? (initial > 0 ? Math.round((invested / initial) * 10000) / 100 : 0)
  return { initial, current, cash, invested, utilization_pct }
}
