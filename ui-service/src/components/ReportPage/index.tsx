import { useState, useEffect } from 'react'
import {
  PieChart, Pie, Cell, Tooltip, Legend, ResponsiveContainer,
  BarChart, Bar, XAxis, YAxis, CartesianGrid, ReferenceLine,
} from 'recharts'
import {
  fetchMonthReport, fetchCumulativeReport,
  type MonthReport, type CumulativeReport, type MonthlyBreakdown,
} from '../../api/report'
import type { Trade } from '../../types'
import { Badge } from '../shared/Badge'
import { parseDate } from '../../utils/date'

type ReportMode = 'all' | 'simulation' | 'live'
type ReportView = 'monthly' | 'cumulative'

const REASON_COLORS: Record<string, string> = {
  TRAIL_STOP:    '#10b981',
  CLOSED:        '#3b82f6',
  SESSION_CLOSE: '#6b7280',
  STOP_LOSS:     '#ef4444',
  DELTA_ERODED:  '#f59e0b',
  IV_CRUSH:      '#8b5cf6',
  STOPPED:       '#ef4444',
  PA_SUPPORT:    '#10b981',
  PA_RESISTANCE: '#10b981',
}

function reasonColor(r: string) {
  return REASON_COLORS[r] ?? '#94a3b8'
}

function todayISO(): string {
  return new Date().toISOString().slice(0, 10)
}

function daysAgoISO(days: number): string {
  const d = new Date()
  d.setDate(d.getDate() - days)
  return d.toISOString().slice(0, 10)
}

function defaultMonth(): string {
  const now = new Date()
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`
}

export function ReportPage() {
  const [view, setView]             = useState<ReportView>('monthly')
  const [modeFilter, setModeFilter] = useState<ReportMode>('all')

  // Monthly state
  const [month, setMonth]           = useState(defaultMonth())
  const [monthReport, setMonthReport] = useState<MonthReport | null>(null)

  // Cumulative state
  const [customRange, setCustomRange] = useState(false)
  const [fromDate, setFromDate]       = useState(daysAgoISO(365))
  const [toDate, setToDate]           = useState(todayISO())
  const [cumReport, setCumReport]     = useState<CumulativeReport | null>(null)

  const [loading, setLoading] = useState(false)
  const [error, setError]     = useState<string | null>(null)

  const tradingMode = modeFilter === 'all' ? undefined : modeFilter

  useEffect(() => {
    if (view !== 'monthly') return
    setLoading(true)
    setError(null)
    fetchMonthReport(month, tradingMode)
      .then(setMonthReport)
      .catch((e) => setError(e.message ?? 'Failed to load report'))
      .finally(() => setLoading(false))
  }, [view, month, modeFilter])

  useEffect(() => {
    if (view !== 'cumulative') return
    setLoading(true)
    setError(null)
    fetchCumulativeReport(fromDate, toDate, tradingMode)
      .then(setCumReport)
      .catch((e) => setError(e.message ?? 'Failed to load report'))
      .finally(() => setLoading(false))
  }, [view, fromDate, toDate, modeFilter])

  const report = view === 'monthly' ? monthReport : cumReport
  const summary = report?.summary ?? null

  return (
    <div className="min-h-screen bg-gray-950 text-gray-100 p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-xl font-bold text-white">Trade Report</h1>
          <p className="text-sm text-gray-500 mt-0.5">P&L analysis with trade reasoning</p>
        </div>

        <div className="flex items-center gap-3 flex-wrap">
          {/* View toggle */}
          <div className="flex bg-gray-800 rounded-md p-0.5 gap-0.5">
            {(['monthly', 'cumulative'] as ReportView[]).map((v) => (
              <button
                key={v}
                onClick={() => setView(v)}
                className={`px-3 py-1.5 text-xs rounded font-mono transition-colors ${
                  view === v ? 'bg-gray-600 text-white' : 'text-gray-400 hover:text-gray-200'
                }`}
              >
                {v === 'monthly' ? 'Monthly' : 'Cumulative'}
              </button>
            ))}
          </div>

          {/* Mode filter */}
          <div className="flex bg-gray-800 rounded-md p-0.5 gap-0.5">
            {(['all', 'simulation', 'live'] as ReportMode[]).map((m) => (
              <button
                key={m}
                onClick={() => setModeFilter(m)}
                className={`px-3 py-1.5 text-xs rounded font-mono transition-colors ${
                  modeFilter === m
                    ? m === 'live'
                      ? 'bg-amber-600 text-white'
                      : m === 'simulation'
                      ? 'bg-blue-600 text-white'
                      : 'bg-gray-600 text-white'
                    : 'text-gray-400 hover:text-gray-200'
                }`}
              >
                {m === 'all' ? 'Cumulative' : m === 'simulation' ? 'Simulation' : 'Live'}
              </button>
            ))}
          </div>

          {/* Date controls */}
          {view === 'monthly' ? (
            <input
              type="month"
              value={month}
              onChange={(e) => setMonth(e.target.value)}
              className="bg-gray-800 border border-gray-700 rounded-md px-3 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
            />
          ) : (
            <div className="flex items-center gap-2">
              {!customRange ? (
                <>
                  <span className="text-xs text-gray-500 font-mono">Last 365 days</span>
                  <button
                    onClick={() => setCustomRange(true)}
                    className="text-xs text-blue-400 hover:text-blue-300 underline"
                  >
                    Custom range
                  </button>
                </>
              ) : (
                <>
                  <input
                    type="date"
                    value={fromDate}
                    onChange={(e) => setFromDate(e.target.value)}
                    className="bg-gray-800 border border-gray-700 rounded-md px-2 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
                  />
                  <span className="text-gray-600 text-xs">to</span>
                  <input
                    type="date"
                    value={toDate}
                    onChange={(e) => setToDate(e.target.value)}
                    className="bg-gray-800 border border-gray-700 rounded-md px-2 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-blue-500"
                  />
                  <button
                    onClick={() => {
                      setCustomRange(false)
                      setFromDate(daysAgoISO(365))
                      setToDate(todayISO())
                    }}
                    className="text-xs text-gray-500 hover:text-gray-300"
                  >
                    Reset
                  </button>
                </>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Date range label for cumulative */}
      {view === 'cumulative' && cumReport && (
        <p className="text-xs text-gray-600 font-mono -mt-2">
          {cumReport.from_date} → {cumReport.to_date}
        </p>
      )}

      {loading && (
        <div className="text-center text-gray-500 py-20 text-sm">Loading report…</div>
      )}
      {error && (
        <div className="text-center text-red-400 py-20 text-sm">{error}</div>
      )}

      {report && !loading && summary && (
        <>
          {/* Summary cards */}
          <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-7 gap-3">
            <StatCard label="Total Trades"  value={summary.total_trades} />
            <StatCard label="Winners"       value={summary.winners}      color="text-emerald-400" />
            <StatCard label="Losers"        value={summary.losers}       color="text-red-400" />
            <StatCard label="Win Rate"      value={`${summary.win_rate}%`} color={summary.win_rate >= 50 ? 'text-emerald-400' : 'text-red-400'} />
            <StatCard label="Net P&L"       value={`₹${summary.net_pnl.toFixed(2)}`} color={summary.net_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'} />
            <StatCard label="Gross Profit"  value={`₹${summary.gross_profit.toFixed(2)}`} color="text-emerald-400" />
            <StatCard label="Gross Loss"    value={`₹${summary.gross_loss.toFixed(2)}`}   color="text-red-400" />
          </div>

          {/* Charts */}
          {view === 'cumulative' && 'by_month' in report && Object.keys(report.by_month).length > 1 && (
            <ChartCard title="Monthly P&L">
              <MonthlyPnlChart byMonth={report.by_month} />
            </ChartCard>
          )}

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <ChartCard title="P&L by Exit Reason">
              <PnlPieChart breakdown={report.by_exit_reason} />
            </ChartCard>
            <ChartCard title="Trade Count by Exit Reason">
              <CountPieChart breakdown={report.by_exit_reason} />
            </ChartCard>
          </div>

          {/* Trade table */}
          <TradeTable trades={report.trades} showMode={modeFilter === 'all'} />
        </>
      )}
    </div>
  )
}

// ── Sub-components ────────────────────────────────────────────────────────────

function StatCard({ label, value, color = 'text-white' }: { label: string; value: string | number; color?: string }) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-3">
      <p className="text-xs text-gray-500 uppercase tracking-wide mb-1">{label}</p>
      <p className={`text-lg font-semibold font-mono ${color}`}>{value}</p>
    </div>
  )
}

function ChartCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <h3 className="text-sm font-semibold text-gray-400 uppercase tracking-wide mb-4">{title}</h3>
      {children}
    </div>
  )
}

function MonthlyPnlChart({ byMonth }: { byMonth: Record<string, MonthlyBreakdown> }) {
  const data = Object.entries(byMonth)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([month, v]) => ({
      month: month.slice(0, 7),
      pnl: v.net_pnl,
      win_rate: v.win_rate,
      trades: v.total_trades,
    }))

  return (
    <ResponsiveContainer width="100%" height={240}>
      <BarChart data={data} margin={{ top: 4, right: 16, left: 0, bottom: 4 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" vertical={false} />
        <XAxis
          dataKey="month"
          tick={{ fill: '#6b7280', fontSize: 11 }}
          axisLine={false}
          tickLine={false}
        />
        <YAxis
          tick={{ fill: '#6b7280', fontSize: 11 }}
          axisLine={false}
          tickLine={false}
          tickFormatter={(v) => `₹${(v / 1000).toFixed(0)}k`}
        />
        <ReferenceLine y={0} stroke="#374151" strokeWidth={1} />
        <Tooltip
          contentStyle={{ background: '#1f2937', border: '1px solid #374151', borderRadius: 6 }}
          labelStyle={{ color: '#9ca3af' }}
          formatter={(value: number, _name: string, props: any) => {
            const { trades, win_rate } = props.payload
            return [
              `${value >= 0 ? '+' : ''}₹${value.toFixed(2)}  (${trades} trades, ${win_rate}% WR)`,
              'Net P&L',
            ]
          }}
        />
        <Bar dataKey="pnl" radius={[3, 3, 0, 0]}>
          {data.map((entry) => (
            <Cell key={entry.month} fill={entry.pnl >= 0 ? '#10b981' : '#ef4444'} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  )
}

function PnlPieChart({ breakdown }: { breakdown: Record<string, { pnl: number; count: number }> }) {
  const data = Object.entries(breakdown)
    .map(([reason, v]) => ({ name: reason, value: Math.abs(v.pnl), raw: v.pnl }))
    .filter((d) => d.value > 0)
    .sort((a, b) => b.value - a.value)

  return (
    <ResponsiveContainer width="100%" height={260}>
      <PieChart>
        <Pie
          data={data}
          cx="50%"
          cy="50%"
          innerRadius={60}
          outerRadius={100}
          paddingAngle={2}
          dataKey="value"
        >
          {data.map((entry) => (
            <Cell key={entry.name} fill={reasonColor(entry.name)} opacity={entry.raw < 0 ? 0.6 : 1} />
          ))}
        </Pie>
        <Tooltip
          contentStyle={{ background: '#1f2937', border: '1px solid #374151', borderRadius: 6 }}
          formatter={(value: number, name: string, props: any) => {
            const raw: number = props.payload.raw
            return [`${raw >= 0 ? '+' : ''}₹${raw.toFixed(2)}`, name]
          }}
          labelStyle={{ color: '#9ca3af' }}
          itemStyle={{ color: '#e5e7eb' }}
        />
        <Legend
          formatter={(value) => <span className="text-xs text-gray-400">{value}</span>}
          wrapperStyle={{ fontSize: 12 }}
        />
      </PieChart>
    </ResponsiveContainer>
  )
}

function CountPieChart({ breakdown }: { breakdown: Record<string, { count: number; wins: number; losses: number }> }) {
  const data = Object.entries(breakdown)
    .map(([reason, v]) => ({ name: reason, value: v.count }))
    .sort((a, b) => b.value - a.value)

  return (
    <ResponsiveContainer width="100%" height={260}>
      <PieChart>
        <Pie
          data={data}
          cx="50%"
          cy="50%"
          innerRadius={60}
          outerRadius={100}
          paddingAngle={2}
          dataKey="value"
          label={({ name, value }) => `${value}`}
          labelLine={false}
        >
          {data.map((entry) => (
            <Cell key={entry.name} fill={reasonColor(entry.name)} />
          ))}
        </Pie>
        <Tooltip
          contentStyle={{ background: '#1f2937', border: '1px solid #374151', borderRadius: 6 }}
          formatter={(value: number, name: string) => [value, name]}
          labelStyle={{ color: '#9ca3af' }}
          itemStyle={{ color: '#e5e7eb' }}
        />
        <Legend
          formatter={(value) => <span className="text-xs text-gray-400">{value}</span>}
          wrapperStyle={{ fontSize: 12 }}
        />
      </PieChart>
    </ResponsiveContainer>
  )
}

const PAGE_SIZE = 25

function TradeTable({ trades, showMode }: { trades: Trade[]; showMode: boolean }) {
  const closed = trades.filter((t) => t.status !== 'OPEN').reverse()
  const [page, setPage] = useState(1)
  const totalPages = Math.ceil(closed.length / PAGE_SIZE)
  const paginated = closed.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)

  if (!closed.length) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-6 text-center text-gray-600 text-sm">
        No closed trades in this period
      </div>
    )
  }

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
      <div className="px-4 py-3 border-b border-gray-800 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-400 uppercase tracking-wide">
          All Trades — {closed.length} closed
        </h3>
        {totalPages > 1 && (
          <span className="text-xs text-gray-500">
            Page {page} of {totalPages}
          </span>
        )}
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-gray-500 uppercase border-b border-gray-800">
              <th className="px-4 py-2 text-left">Date</th>
              <th className="px-4 py-2 text-left">Symbol</th>
              <th className="px-4 py-2 text-right">Entry</th>
              <th className="px-4 py-2 text-right">Exit</th>
              <th className="px-4 py-2 text-right">P&L</th>
              <th className="px-4 py-2 text-right">%</th>
              <th className="px-4 py-2 text-left">Exit Reason</th>
              {showMode && <th className="px-4 py-2 text-left">Mode</th>}
              <th className="px-4 py-2 text-left w-80">Why this trade was made</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800/60">
            {paginated.map((t) => {
              const profit = (t.pnl ?? 0) > 0
              const rowBg = profit ? 'bg-emerald-950/20' : 'bg-red-950/20'
              const pnlColor = profit ? 'text-emerald-400' : 'text-red-400'
              const symbol = (t.option_symbol ?? t.symbol).replace('NSE:', '')
              const entryTs = parseDate(t.entry_time)
              const date = entryTs.toLocaleDateString('en-IN', {
                day: '2-digit', month: 'short', year: '2-digit',
              })
              const entryTime = entryTs.toLocaleTimeString('en-IN', {
                hour: '2-digit', minute: '2-digit', hour12: false,
              })
              return (
                <tr key={t.trade_id} className={`${rowBg} hover:brightness-125 transition-all`}>
                  <td className="px-4 py-2.5 text-gray-400 whitespace-nowrap">
                    {date} {entryTime}
                  </td>
                  <td className="px-4 py-2.5">
                    <div className="flex items-center gap-1.5">
                      <Badge label={t.side} variant={t.side === 'BUY' ? 'buy' : 'sell'} />
                      <span className="font-mono text-gray-200">{symbol}</span>
                    </div>
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-gray-300">
                    ₹{t.entry_price.toFixed(2)}
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-gray-300">
                    {t.exit_price != null ? `₹${t.exit_price.toFixed(2)}` : '—'}
                  </td>
                  <td className={`px-4 py-2.5 text-right font-mono font-semibold ${pnlColor}`}>
                    {t.pnl != null ? `${t.pnl > 0 ? '+' : ''}₹${t.pnl.toFixed(2)}` : '—'}
                  </td>
                  <td className={`px-4 py-2.5 text-right font-mono font-semibold ${pnlColor}`}>
                    {t.pnl_pct != null ? `${t.pnl_pct > 0 ? '+' : ''}${t.pnl_pct.toFixed(2)}%` : '—'}
                  </td>
                  <td className="px-4 py-2.5">
                    <ReasonBadge reason={t.exit_reason ?? t.status} />
                  </td>
                  {showMode && (
                    <td className="px-4 py-2.5">
                      <ModeBadge mode={t.trading_mode} />
                    </td>
                  )}
                  <td className="px-4 py-2.5 text-gray-400 leading-relaxed max-w-xs">
                    {t.reasoning ?? '—'}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {totalPages > 1 && (
        <div className="px-4 py-3 border-t border-gray-800 flex items-center justify-between">
          <span className="text-xs text-gray-500">
            {(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, closed.length)} of {closed.length} trades
          </span>
          <div className="flex gap-2">
            <button
              onClick={() => setPage((p) => p - 1)}
              disabled={page === 1}
              className="px-3 py-1 text-xs rounded bg-gray-800 text-gray-300 disabled:opacity-30 hover:bg-gray-700 transition-colors"
            >
              Prev
            </button>
            <button
              onClick={() => setPage((p) => p + 1)}
              disabled={page === totalPages}
              className="px-3 py-1 text-xs rounded bg-gray-800 text-gray-300 disabled:opacity-30 hover:bg-gray-700 transition-colors"
            >
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

function ReasonBadge({ reason }: { reason: string }) {
  const color = REASON_COLORS[reason]
  const style = color
    ? { backgroundColor: `${color}22`, color, borderColor: `${color}44` }
    : { backgroundColor: '#374151', color: '#9ca3af' }
  return (
    <span
      className="inline-block px-1.5 py-0.5 rounded text-xs font-medium border"
      style={style}
    >
      {reason}
    </span>
  )
}

function ModeBadge({ mode }: { mode?: string }) {
  if (mode === 'live') {
    return (
      <span className="inline-block px-1.5 py-0.5 rounded text-xs font-mono font-medium border bg-amber-500/10 text-amber-400 border-amber-500/30">
        LIVE
      </span>
    )
  }
  return (
    <span className="inline-block px-1.5 py-0.5 rounded text-xs font-mono font-medium border bg-blue-500/10 text-blue-400 border-blue-500/30">
      SIM
    </span>
  )
}
