import { apiClient } from './client'
import type { Position } from '../types'

export async function fetchPositions(): Promise<{ positions: Position[]; summary: { total_positions: number; total_invested: number } }> {
  const res = await apiClient.get('/positions')
  return res.data.data
}
