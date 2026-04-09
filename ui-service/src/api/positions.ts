import { apiClient } from './client'
import type { Position } from '../types'

export async function fetchPositions(): Promise<{ positions: Position[]; summary: { total_positions: number; total_invested: number } }> {
  const res = await apiClient.get('/positions')
  return res.data.data
}

export async function closePosition(symbol: string): Promise<{ trade_id: string; exit_price: number; pnl: number }> {
  const res = await apiClient.post(`/positions/${encodeURIComponent(symbol)}/close`)
  return res.data.data
}
