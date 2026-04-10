import { apiClient } from './client'
import type { Decision } from '../types'

export async function fetchDecisions(params?: { symbol?: string; decision?: string; limit?: number }): Promise<{ total: number; decisions: Decision[] }> {
  const res = await apiClient.get('/decision-log', { params })
  return res.data.data
}

export async function fetchDecisionById(decisionId: string): Promise<Decision | null> {
  try {
    const res = await apiClient.get(`/decisions/${decisionId}`)
    return res.data.decision ?? null
  } catch {
    return null
  }
}
