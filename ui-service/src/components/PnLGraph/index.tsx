import { AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts'
import { format } from 'date-fns'
import type { PnLData } from '../../types'

interface Props {
  pnl: PnLData
}

export function PnLGraph({ pnl }: Props) {
  const timelineData = pnl.timeline.map((t) => ({
    time: format(new Date(t.timestamp), 'HH:mm'),
    pnl: t.cumulative_pnl,
  }))

  const isPositive = pnl.total_pnl >= 0

  return (
    <div className="bg-gray-900 rounded-lg p-4 border border-gray-800">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wide">P&L Today</h2>
        <div className="text-right">
          <div className={`text-2xl font-bold ${isPositive ? 'text-emerald-400' : 'text-red-400'}`}>
            {isPositive ? '+' : ''}₹{pnl.total_pnl.toLocaleString('en-IN', { minimumFractionDigits: 2 })}
          </div>
          <div className={`text-sm ${isPositive ? 'text-emerald-500' : 'text-red-500'}`}>
            {isPositive ? '+' : ''}{pnl.total_pnl_pct.toFixed(2)}%
          </div>
        </div>
      </div>

      <div className="grid grid-cols-4 gap-3 mb-4 text-center">
        <Stat label="Win Rate" value={`${(pnl.win_rate * 100).toFixed(0)}%`} />
        <Stat label="Avg Win" value={`₹${pnl.avg_win.toFixed(0)}`} positive />
        <Stat label="Avg Loss" value={`₹${pnl.avg_loss.toFixed(0)}`} negative />
        <Stat label="Total Trades" value={`${pnl.total_trades}`} />
      </div>

      <ResponsiveContainer width="100%" height={160}>
        <AreaChart data={timelineData}>
          <defs>
            <linearGradient id="pnlGradient" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor={isPositive ? '#3fb950' : '#f85149'} stopOpacity={0.3} />
              <stop offset="95%" stopColor={isPositive ? '#3fb950' : '#f85149'} stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="#21262d" />
          <XAxis dataKey="time" tick={{ fill: '#8b949e', fontSize: 11 }} />
          <YAxis tick={{ fill: '#8b949e', fontSize: 11 }} tickFormatter={(v) => `₹${v.toFixed(0)}`} />
          <Tooltip
            contentStyle={{ background: '#161b22', border: '1px solid #30363d', borderRadius: 8 }}
            labelStyle={{ color: '#8b949e' }}
            formatter={(v: number) => [`₹${v.toFixed(2)}`, 'P&L']}
          />
          <Area
            type="monotone"
            dataKey="pnl"
            stroke={isPositive ? '#3fb950' : '#f85149'}
            fill="url(#pnlGradient)"
            strokeWidth={2}
          />
        </AreaChart>
      </ResponsiveContainer>

      {/* Budget bar */}
      <div className="mt-4">
        <div className="flex justify-between text-xs text-gray-500 mb-1">
          <span>Capital Utilization</span>
          <span>{pnl.budget.utilization_pct.toFixed(1)}%</span>
        </div>
        <div className="h-1.5 bg-gray-800 rounded-full overflow-hidden">
          <div
            className="h-full bg-blue-500 rounded-full transition-all duration-500"
            style={{ width: `${Math.min(pnl.budget.utilization_pct, 100)}%` }}
          />
        </div>
        <div className="flex justify-between text-xs text-gray-500 mt-1">
          <span>Cash: ₹{pnl.budget.cash.toLocaleString('en-IN', { maximumFractionDigits: 0 })}</span>
          <span>Invested: ₹{pnl.budget.invested.toLocaleString('en-IN', { maximumFractionDigits: 0 })}</span>
        </div>
      </div>
    </div>
  )
}

function Stat({ label, value, positive, negative }: { label: string; value: string; positive?: boolean; negative?: boolean }) {
  return (
    <div className="bg-gray-800/50 rounded p-2">
      <div className="text-xs text-gray-500 mb-0.5">{label}</div>
      <div className={`text-sm font-semibold ${positive ? 'text-emerald-400' : negative ? 'text-red-400' : 'text-gray-200'}`}>
        {value}
      </div>
    </div>
  )
}
