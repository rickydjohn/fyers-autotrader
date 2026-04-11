import { apiClient } from './client'

export type CheckStatus = 'ok' | 'degraded' | 'unavailable' | 'error'

export interface ServiceHealth {
  status: CheckStatus
  checks: Record<string, CheckStatus>
  error?: string
}

export interface SystemHealth {
  status: CheckStatus
  services: {
    api_service: ServiceHealth
    core_engine: ServiceHealth
    simulation_engine: ServiceHealth
    data_service: ServiceHealth
  }
  checked_at: string
}

export async function fetchSystemHealth(): Promise<SystemHealth> {
  const res = await apiClient.get('/health')
  return res.data
}
