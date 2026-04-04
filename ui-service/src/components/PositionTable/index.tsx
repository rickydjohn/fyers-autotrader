import { Badge } from '../shared/Badge'
import type { Position } from '../../types'

interface Props {
  positions: Position[]
}

export function PositionTable({ positions }: Props) {
  if (!positions.length) {
    return (
      <div className="bg-gray-900 rounded-lg border border-gray-800 p-6 text-center text-gray-600 text-sm">
        No open positions
      </div>
    )
  }

  return (
    <div className="bg-gray-900 rounded-lg border border-gray-800 overflow-hidden">
      <div className="px-4 py-3 border-b border-gray-800">
        <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wide">
          Open Positions ({positions.length})
        </h2>
      </div>
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
    </div>
  )
}
