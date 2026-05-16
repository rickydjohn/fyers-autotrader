import { apiClient } from './client'
import type { HistoricalCandle, ContextSnapshot, OptionsChain, Timeframe, Decision } from '../types'

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

/**
 * Fetches the in-progress 1m bar accumulated from WS ticks, plus the just-
 * finalised previous bar (if any). Returns nulls when the market is closed
 * or the feed is unavailable — the chart should treat those as "no update"
 * rather than clearing existing data.
 */
export interface FormingBarResponse {
  forming_bar: HistoricalCandle | null
  last_bar: HistoricalCandle | null
}

export async function fetchFormingBar(symbol: string): Promise<FormingBarResponse> {
  try {
    const res = await apiClient.get('/market-data/forming-bar', { params: { symbol } })
    const data = res.data?.data ?? res.data
    return {
      forming_bar: data?.forming_bar ?? null,
      last_bar:    data?.last_bar    ?? null,
    }
  } catch {
    // 404 (market closed) or any transport error — silently report "no data".
    return { forming_bar: null, last_bar: null }
  }
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

export async function fetchOptionsChain(symbol: string): Promise<OptionsChain | null> {
  try {
    const res = await apiClient.get('/options-chain', { params: { symbol } })
    return res.data.data ?? null
  } catch {
    return null
  }
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
  // Normalize: decision-history endpoint returns `time`, but Decision type uses `timestamp`
  const decisions: Decision[] = (res.data.decisions ?? []).map((d: any) => ({
    ...d,
    timestamp: d.timestamp ?? d.time,
  }))
  return { ...res.data, decisions }
}
