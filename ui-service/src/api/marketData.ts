import { apiClient } from './client'
import type { MarketData } from '../types'

export async function fetchMarketData(symbol: string): Promise<MarketData> {
  const res = await apiClient.get('/market-data', { params: { symbol } })
  return res.data.data
}

export async function fetchSymbols(): Promise<string[]> {
  const res = await apiClient.get('/market-data/symbols')
  return res.data.data.symbols
}
