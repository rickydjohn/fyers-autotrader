import { useState } from 'react'
import { Badge } from '../shared/Badge'
import type { Position, Trade } from '../../types'

interface Props {
  positions: Position[]
  trades: Trade[]
}

export function PositionTable({ positions, trades }: Props) {
  const [view, setView] = useState<'positions' | 'trades'>('positions')

  return (
    <div className="bg-gray-900 rounded-lg border border-gray-800 overflow-hidden">
      {/* Header with toggle */}
      <div className="px-4 py-3 border-b border-gray-800 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wide">
          {view === 'positions'
            ? `Open Positions (${positions.length})`
            : `Today's Trades (${trades.length})`}
        </h2>
        <div className="flex items-center bg-gray-800 rounded-md p-0.5 gap-0.5">
          <button
            onClick={() => setView('positions')}
            className={`px-3 py-1 text-xs font-medium rounded transition-colors ${
              view === 'positions'
                ? 'bg-gray-600 text-white'
                : 'text-gray-400 hover:text-gray-200'
            }`}
          >
            Positions
          </button>
          <button
            onClick={() => setView('trades')}
            className={`px-3 py-1 text-xs font-medium rounded transition-colors ${
              view === 'trades'
                ? 'bg-gray-600 text-white'
                : 'text-gray-400 hover:text-gray-200'
            }`}
          >
            Trades
          </button>
        </div>
      </div>

      {view === 'positions' ? (
        <PositionsView positions={positions} />
      ) : (
        <TradesView trades={trades} />
      )}
    </div>
  )
}

function PositionsView({ positions }: { positions: Position[] }) {
  if (!positions.length) {
    return (
      <div className="p-6 text-center text-gray-600 text-sm">
        No open positions
      </div>
    )
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-xs text-gray-500 uppercase">
            <th className="px-4 py-2 text-left">Symbol</th>
            <th className="px-4 py-2 text-left">Option</th>
            <th className="px-4 py-2 text-right">Strike</th>
            <th className="px-4 py-2 text-right">Qty</th>
            <th className="px-4 py-2 text-right">Avg</th>
            <th className="px-4 py-2 text-right">LTP</th>
            <th className="px-4 py-2 text-right">P&L</th>
            <th className="px-4 py-2 text-right">SL / Target</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-800">
          {positions.map((pos) => {
            const pnl = pos.unrealized_pnl ?? 0
            const isPositive = pnl >= 0
            const optionLabel = pos.option_symbol
              ? pos.option_symbol.replace('NSE:', '')
              : '—'
            return (
              <tr key={pos.symbol} className="hover:bg-gray-800/40">
                <td className="px-4 py-3">
                  <div className="flex items-center gap-2">
                    <Badge label={pos.side} variant={pos.side === 'BUY' ? 'buy' : 'sell'} />
                    <span className="font-mono text-blue-400">{pos.symbol.replace('NSE:', '')}</span>
                  </div>
                </td>
                <td className="px-4 py-3">
                  {pos.option_symbol ? (
                    <span className="font-mono text-xs text-amber-400">{optionLabel}</span>
                  ) : (
                    <span className="text-gray-600 text-xs">—</span>
                  )}
                </td>
                <td className="px-4 py-3 text-right font-mono text-xs text-gray-300">
                  {pos.option_strike ? (
                    <span>
                      ₹{pos.option_strike.toLocaleString('en-IN')}
                      {pos.option_type && (
                        <span className={`ml-1 font-semibold ${pos.option_type === 'CE' ? 'text-emerald-400' : 'text-red-400'}`}>
                          {pos.option_type}
                        </span>
                      )}
                    </span>
                  ) : '—'}
                </td>
                <td className="px-4 py-3 text-right font-mono text-gray-300">{pos.quantity}</td>
                <td className="px-4 py-3 text-right font-mono text-gray-300">₹{pos.avg_price.toLocaleString('en-IN')}</td>
                <td className="px-4 py-3 text-right font-mono text-gray-200">
                  {pos.ltp ? `₹${pos.ltp.toLocaleString('en-IN')}` : '-'}
                </td>
                <td className={`px-4 py-3 text-right font-mono font-semibold ${isPositive ? 'text-emerald-400' : 'text-red-400'}`}>
                  {isPositive ? '+' : ''}₹{pnl.toFixed(2)}
                  {pos.unrealized_pnl_pct !== undefined && (
                    <span className="text-xs ml-1">({isPositive ? '+' : ''}{pos.unrealized_pnl_pct.toFixed(2)}%)</span>
                  )}
                </td>
                <td className="px-4 py-3 text-right text-xs font-mono">
                  <span className="text-red-400">₹{pos.stop_loss.toLocaleString('en-IN')}</span>
                  <span className="text-gray-600 mx-1">/</span>
                  <span className="text-emerald-400">₹{pos.target.toLocaleString('en-IN')}</span>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function TradesView({ trades }: { trades: Trade[] }) {
  if (!trades.length) {
    return (
      <div className="p-6 text-center text-gray-600 text-sm">
        No trades today
      </div>
    )
  }

  const totalPnl = trades.reduce((sum, t) => sum + (t.pnl ?? 0), 0)
  const closed = trades.filter((t) => t.status !== 'OPEN')
  const winners = closed.filter((t) => (t.pnl ?? 0) > 0).length
  const losers = closed.filter((t) => (t.pnl ?? 0) <= 0).length

  return (
    <>
      {/* Summary bar */}
      <div className="px-4 py-2 border-b border-gray-800 flex items-center gap-4 text-xs text-gray-400">
        <span>{closed.length} closed</span>
        <span className="text-emerald-400">{winners}W</span>
        <span className="text-red-400">{losers}L</span>
        <span className={`ml-auto font-semibold font-mono ${totalPnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
          Net {totalPnl >= 0 ? '+' : ''}₹{totalPnl.toFixed(2)}
        </span>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs text-gray-500 uppercase">
              <th className="px-4 py-2 text-left">Symbol</th>
              <th className="px-4 py-2 text-right">Entry</th>
              <th className="px-4 py-2 text-right">Exit</th>
              <th className="px-4 py-2 text-right">Qty</th>
              <th className="px-4 py-2 text-right">P&L</th>
              <th className="px-4 py-2 text-right">%</th>
              <th className="px-4 py-2 text-left">Status</th>
              <th className="px-4 py-2 text-right">Time</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800">
            {trades.map((trade) => {
              const isOpen = trade.status === 'OPEN'
              const pnl = trade.pnl ?? 0
              const isProfit = pnl > 0
              const rowColor = isOpen
                ? 'hover:bg-gray-800/40'
                : isProfit
                  ? 'bg-emerald-950/30 hover:bg-emerald-950/50'
                  : 'bg-red-950/30 hover:bg-red-950/50'
              const pnlColor = isOpen
                ? 'text-gray-400'
                : isProfit
                  ? 'text-emerald-400'
                  : 'text-red-400'
              const symbol = (trade.option_symbol ?? trade.symbol).replace('NSE:', '')
              const entryTime = new Date(trade.entry_time).toLocaleTimeString('en-IN', {
                hour: '2-digit', minute: '2-digit', hour12: false,
              })

              return (
                <tr key={trade.trade_id} className={`${rowColor} transition-colors`}>
                  <td className="px-4 py-2.5">
                    <div className="flex items-center gap-2">
                      <Badge label={trade.side} variant={trade.side === 'BUY' ? 'buy' : 'sell'} />
                      <span className="font-mono text-xs text-gray-200">{symbol}</span>
                    </div>
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-xs text-gray-300">
                    ₹{trade.entry_price.toFixed(2)}
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-xs text-gray-300">
                    {trade.exit_price != null ? `₹${trade.exit_price.toFixed(2)}` : '—'}
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-xs text-gray-400">
                    {trade.quantity}
                  </td>
                  <td className={`px-4 py-2.5 text-right font-mono text-xs font-semibold ${pnlColor}`}>
                    {isOpen ? '—' : `${isProfit ? '+' : ''}₹${pnl.toFixed(2)}`}
                  </td>
                  <td className={`px-4 py-2.5 text-right font-mono text-xs font-semibold ${pnlColor}`}>
                    {isOpen || trade.pnl_pct == null
                      ? '—'
                      : `${isProfit ? '+' : ''}${trade.pnl_pct.toFixed(2)}%`}
                  </td>
                  <td className="px-4 py-2.5">
                    <StatusBadge status={trade.status} exitReason={trade.exit_reason} />
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-xs text-gray-500">
                    {entryTime}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </>
  )
}

function StatusBadge({ status, exitReason }: { status: string; exitReason?: string }) {
  const label = exitReason ?? status
  const color =
    status === 'OPEN'
      ? 'bg-blue-900/50 text-blue-300'
      : label === 'TRAIL_STOP'
        ? 'bg-emerald-900/50 text-emerald-300'
        : label === 'CLOSED'
          ? 'bg-emerald-900/50 text-emerald-300'
          : 'bg-red-900/50 text-red-300'

  return (
    <span className={`inline-block px-1.5 py-0.5 rounded text-xs font-medium ${color}`}>
      {label}
    </span>
  )
}
