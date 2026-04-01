import { apiClient } from './client'
import type { PnLData } from '../types'

export async function fetchPnL(period = 'today'): Promise<PnLData> {
  const res = await apiClient.get('/pnl', { params: { period } })
  return res.data.data
}
