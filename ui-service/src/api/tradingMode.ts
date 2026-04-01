import { apiClient } from './client'
import type { FundsData, TradingMode } from '../types'

export async function fetchTradingMode(): Promise<{ mode: TradingMode }> {
  const r = await apiClient.get('/api/v1/trading-mode')
  return r.data
}

export async function updateTradingMode(mode: TradingMode): Promise<{ mode: TradingMode }> {
  const r = await apiClient.post('/api/v1/trading-mode', null, { params: { mode } })
  return r.data
}

export async function fetchFunds(): Promise<FundsData> {
  const r = await apiClient.get('/api/v1/funds')
  return r.data.funds ?? {}
}
