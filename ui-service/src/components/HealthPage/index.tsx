import { useEffect, useState, useCallback } from 'react'
import { fetchSystemHealth } from '../../api/health'
import type { SystemHealth, CheckStatus } from '../../api/health'

const REFRESH_INTERVAL_MS = 15_000

const SERVICE_LABELS: Record<string, string> = {
  api_service:       'API Service',
  core_engine:       'Core Engine',
  simulation_engine: 'Simulation Engine',
  data_service:      'Data Service',
}

const CHECK_LABELS: Record<string, string> = {
  redis:        'Redis',
  timescaledb:  'TimescaleDB',
  ollama:       'Ollama (LLM)',
  fyers_auth:   'Fyers Auth',
  data_service: 'Data Service',
}

function StatusDot({ status }: { status: CheckStatus | 'loading' }) {
  const classes: Record<string, string> = {
    ok:          'bg-emerald-400',
    degraded:    'bg-amber-400',
    unavailable: 'bg-red-500',
    error:       'bg-red-500',
    loading:     'bg-gray-600 animate-pulse',
  }
  return (
    <span className={`inline-block w-2.5 h-2.5 rounded-full ${classes[status] ?? classes.loading}`} />
  )
}

function StatusBadge({ status }: { status: CheckStatus | 'loading' }) {
  const styles: Record<string, string> = {
    ok:          'bg-emerald-500/10 text-emerald-400 border-emerald-500/30',
    degraded:    'bg-amber-500/10  text-amber-400  border-amber-500/30',
    unavailable: 'bg-red-500/10    text-red-400    border-red-500/30',
    error:       'bg-red-500/10    text-red-400    border-red-500/30',
    loading:     'bg-gray-700/50   text-gray-500   border-gray-600/30',
  }
  const labels: Record<string, string> = {
    ok: 'OK', degraded: 'DEGRADED', unavailable: 'UNAVAILABLE', error: 'ERROR', loading: '…',
  }
  return (
    <span className={`text-xs font-mono px-2 py-0.5 rounded border ${styles[status] ?? styles.loading}`}>
      {labels[status] ?? status.toUpperCase()}
    </span>
  )
}

export function HealthPage() {
  const [health, setHealth] = useState<SystemHealth | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null)

  const refresh = useCallback(async () => {
    try {
      const data = await fetchSystemHealth()
      setHealth(data)
      setLastRefreshed(new Date())
      setError(null)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to fetch health data')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refresh()
    const interval = setInterval(refresh, REFRESH_INTERVAL_MS)
    return () => clearInterval(interval)
  }, [refresh])

  const overallStatus = loading ? 'loading' : error ? 'error' : health?.status ?? 'loading'

  return (
    <div className="max-w-3xl mx-auto px-6 py-8 space-y-6">

      {/* Page header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-semibold text-white">System Health</h2>
          <p className="text-xs text-gray-500 mt-0.5">
            {lastRefreshed
              ? `Last checked ${lastRefreshed.toLocaleTimeString()} · auto-refreshes every 15s`
              : 'Checking…'}
          </p>
        </div>
        <button
          onClick={refresh}
          className="text-xs px-3 py-1.5 rounded bg-gray-800 text-gray-400 hover:bg-gray-700 hover:text-gray-200 transition-colors"
        >
          Refresh
        </button>
      </div>

      {/* Overall status banner */}
      <div className={`flex items-center gap-3 px-4 py-3 rounded-lg border ${
        overallStatus === 'ok'
          ? 'bg-emerald-500/10 border-emerald-500/20'
          : overallStatus === 'loading'
          ? 'bg-gray-800 border-gray-700'
          : 'bg-red-500/10 border-red-500/20'
      }`}>
        <StatusDot status={overallStatus as CheckStatus} />
        <span className="text-sm font-medium text-white">
          {overallStatus === 'ok'
            ? 'All systems operational'
            : overallStatus === 'loading'
            ? 'Checking systems…'
            : 'One or more systems degraded'}
        </span>
        {!loading && <StatusBadge status={overallStatus as CheckStatus} />}
      </div>

      {/* Error state */}
      {error && (
        <div className="px-4 py-3 rounded-lg bg-red-500/10 border border-red-500/20 text-red-400 text-sm">
          {error}
        </div>
      )}

      {/* Service cards */}
      {health && Object.entries(health.services).map(([serviceKey, service]) => (
        <div key={serviceKey} className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
          {/* Service header */}
          <div className="flex items-center justify-between px-4 py-3 border-b border-gray-800">
            <div className="flex items-center gap-2.5">
              <StatusDot status={service.status} />
              <span className="text-sm font-medium text-white">
                {SERVICE_LABELS[serviceKey] ?? serviceKey}
              </span>
            </div>
            <StatusBadge status={service.status} />
          </div>

          {/* Sub-checks */}
          <div className="divide-y divide-gray-800/60">
            {Object.entries(service.checks).map(([checkKey, checkStatus]) => (
              <div key={checkKey} className="flex items-center justify-between px-4 py-2.5">
                <div className="flex items-center gap-2">
                  <StatusDot status={checkStatus} />
                  <span className="text-sm text-gray-400">
                    {CHECK_LABELS[checkKey] ?? checkKey}
                  </span>
                </div>
                <StatusBadge status={checkStatus} />
              </div>
            ))}
            {Object.keys(service.checks).length === 0 && (
              <div className="px-4 py-2.5 text-xs text-gray-600 italic">No dependency checks</div>
            )}
          </div>

          {/* Error detail */}
          {service.error && (
            <div className="px-4 py-2 bg-red-950/30 border-t border-red-900/30 text-xs text-red-400 font-mono truncate">
              {service.error}
            </div>
          )}
        </div>
      ))}

      {/* Loading skeleton */}
      {loading && !health && (
        <div className="space-y-4">
          {[...Array(4)].map((_, i) => (
            <div key={i} className="bg-gray-900 border border-gray-800 rounded-lg h-24 animate-pulse" />
          ))}
        </div>
      )}
    </div>
  )
}
