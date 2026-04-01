import { apiClient } from './client'
import type { HistoricalCandle, ContextSnapshot, Timeframe } from '../types'

export async function fetchHistoricalData(
  symbol: string,
  interval: Timeframe,
  limit = 200,
): Promise<HistoricalCandle[]> {
  const res = await apiClient.get('/historical-data', {
    params: { symbol, interval, limit },
  })
  return res.data.candles
}

export async function fetchAggregatedView(
  symbol: string,
  interval: Timeframe,
  limit = 100,
): Promise<HistoricalCandle[]> {
  const res = await apiClient.get('/aggregated-view', {
    params: { symbol, interval, limit },
  })
  return res.data.candles
}

export async function fetchContextSnapshot(symbol: string): Promise<ContextSnapshot> {
  const res = await apiClient.get('/context-snapshot', { params: { symbol } })
  return res.data.context
}

export async function fetchDecisionHistory(
  symbol?: string,
  limit = 100,
  decisionType?: string,
) {
  const params: Record<string, string | number> = { limit }
  if (symbol) params.symbol = symbol
  if (decisionType) params.decision_type = decisionType
  const res = await apiClient.get('/decision-history', { params })
  return res.data
}
