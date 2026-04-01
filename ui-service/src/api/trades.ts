import { apiClient } from './client'
import type { Trade } from '../types'

export async function fetchTrades(symbol?: string, limit = 50): Promise<{ total: number; trades: Trade[] }> {
  const res = await apiClient.get('/trades', { params: { symbol, limit } })
  return res.data.data
}
