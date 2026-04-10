import { useState } from 'react'
import { Badge } from '../shared/Badge'
import type { Decision, Position, Trade } from '../../types'
import { parseDate } from '../../utils/date'
import { closePosition } from '../../api/positions'
import { fetchDecisionById } from '../../api/decisionLog'

interface Props {
  positions: Position[]
  trades: Trade[]
  onPositionClosed?: () => void
}

export function PositionTable({ positions, trades, onPositionClosed }: Props) {
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
        <PositionsView positions={positions} onPositionClosed={onPositionClosed} />
      ) : (
        <TradesView trades={trades} />
      )}
    </div>
  )
}

function PositionsView({
  positions,
  onPositionClosed,
}: {
  positions: Position[]
  onPositionClosed?: () => void
}) {
  const [confirmSymbol, setConfirmSymbol] = useState<string | null>(null)
  const [closing, setClosing] = useState<string | null>(null)

  if (!positions.length) {
    return (
      <div className="p-6 text-center text-gray-600 text-sm">
        No open positions
      </div>
    )
  }

  async function handleExit(symbol: string) {
    if (confirmSymbol !== symbol) {
      setConfirmSymbol(symbol)
      return
    }
    setClosing(symbol)
    setConfirmSymbol(null)
    try {
      await closePosition(symbol)
      onPositionClosed?.()
    } catch {
      // swallow — position refresh will reflect actual state
    } finally {
      setClosing(null)
    }
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
            <th className="px-4 py-2 text-right">Exit</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-800">
          {positions.map((pos) => {
            const pnl = pos.unrealized_pnl ?? 0
            const isPositive = pnl >= 0
            const optionLabel = pos.option_symbol
              ? pos.option_symbol.replace('NSE:', '')
              : '—'
            const isConfirming = confirmSymbol === pos.symbol
            const isClosing = closing === pos.symbol
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
                <td className="px-4 py-3 text-right">
                  {isClosing ? (
                    <span className="text-xs text-gray-500">closing…</span>
                  ) : isConfirming ? (
                    <div className="flex items-center justify-end gap-1">
                      <button
                        onClick={() => handleExit(pos.symbol)}
                        className="px-2 py-0.5 rounded text-xs font-medium bg-red-600 hover:bg-red-500 text-white transition-colors"
                      >
                        Confirm
                      </button>
                      <button
                        onClick={() => setConfirmSymbol(null)}
                        className="px-2 py-0.5 rounded text-xs font-medium text-gray-400 hover:text-gray-200 transition-colors"
                      >
                        Cancel
                      </button>
                    </div>
                  ) : (
                    <button
                      onClick={() => handleExit(pos.symbol)}
                      className="px-2 py-0.5 rounded text-xs font-medium border border-gray-700 text-gray-400 hover:border-red-500 hover:text-red-400 transition-colors"
                    >
                      Exit
                    </button>
                  )}
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
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [decisionCache, setDecisionCache] = useState<Record<string, Decision | null>>({})
  const [loading, setLoading] = useState<string | null>(null)

  async function handleRowClick(trade: Trade) {
    const id = trade.trade_id
    if (expandedId === id) {
      setExpandedId(null)
      return
    }
    setExpandedId(id)
    if (id in decisionCache || !trade.decision_id) return
    setLoading(id)
    const decision = await fetchDecisionById(trade.decision_id)
    setDecisionCache((prev) => ({ ...prev, [id]: decision }))
    setLoading(null)
  }

  if (!trades.length) {
    return <div className="p-6 text-center text-gray-600 text-sm">No trades today</div>
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
              const isExpanded = expandedId === trade.trade_id
              const isLoadingThis = loading === trade.trade_id
              const rowBase = isOpen
                ? ''
                : isProfit
                  ? 'bg-emerald-950/20'
                  : 'bg-red-950/20'
              const pnlColor = isOpen ? 'text-gray-400' : isProfit ? 'text-emerald-400' : 'text-red-400'
              const symbol = (trade.option_symbol ?? trade.symbol).replace('NSE:', '')
              const entryTime = parseDate(trade.entry_time).toLocaleTimeString('en-IN', {
                hour: '2-digit', minute: '2-digit', hour12: false,
              })

              return [
                <tr
                  key={trade.trade_id}
                  onClick={() => handleRowClick(trade)}
                  className={`${rowBase} hover:bg-gray-800/50 transition-colors cursor-pointer select-none`}
                >
                  <td className="px-4 py-2.5">
                    <div className="flex items-center gap-2">
                      <span className={`text-gray-600 text-xs transition-transform ${isExpanded ? 'rotate-90' : ''}`}>▶</span>
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
                    {isOpen || trade.pnl_pct == null ? '—' : `${isProfit ? '+' : ''}${trade.pnl_pct.toFixed(2)}%`}
                  </td>
                  <td className="px-4 py-2.5">
                    <StatusBadge status={trade.status} exitReason={trade.exit_reason} />
                  </td>
                  <td className="px-4 py-2.5 text-right font-mono text-xs text-gray-500">
                    {entryTime}
                  </td>
                </tr>,

                isExpanded && (
                  <tr key={`${trade.trade_id}-detail`} className="bg-gray-900/80">
                    <td colSpan={8} className="px-4 pb-3 pt-0">
                      {isLoadingThis ? (
                        <div className="text-xs text-gray-600 py-2 animate-pulse">Loading decision context…</div>
                      ) : (
                        <TradeDetailPanel trade={trade} decision={decisionCache[trade.trade_id] ?? null} />
                      )}
                    </td>
                  </tr>
                ),
              ]
            })}
          </tbody>
        </table>
      </div>
    </>
  )
}

function TradeDetailPanel({ trade, decision }: { trade: Trade; decision: Decision | null }) {
  const ind = decision?.indicators_snapshot
  const entryDt = parseDate(trade.entry_time)
  const exitDt = trade.exit_time ? parseDate(trade.exit_time) : null
  const durationMs = exitDt ? exitDt.getTime() - entryDt.getTime() : null
  const durationMin = durationMs ? Math.round(durationMs / 60000) : null

  const fmt = (v: number | undefined, decimals = 2) =>
    v != null ? v.toFixed(decimals) : '—'
  const fmtPrice = (v: number | undefined) =>
    v != null ? `₹${Number(v).toLocaleString('en-IN', { minimumFractionDigits: 2 })}` : '—'

  return (
    <div className="mt-1 rounded-lg border border-gray-700/60 bg-gray-800/60 p-3 space-y-3 text-xs">

      {/* Timing row */}
      <div className="flex flex-wrap gap-4 text-gray-400 font-mono">
        <span>Entry <span className="text-gray-200">{entryDt.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })}</span></span>
        {exitDt && <span>Exit <span className="text-gray-200">{exitDt.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })}</span></span>}
        {durationMin != null && <span>Duration <span className="text-gray-200">{durationMin}m</span></span>}
        {decision && <span>Confidence <span className="text-blue-400 font-semibold">{(decision.confidence * 100).toFixed(0)}%</span></span>}
        {decision && <span>R:R <span className="text-gray-200">{decision.risk_reward?.toFixed(2)}</span></span>}
      </div>

      {/* Decision SL / Target vs actual exit */}
      {decision && (
        <div className="flex flex-wrap gap-4 font-mono">
          <span className="text-gray-500">
            Underlying SL{' '}
            <span className="text-red-400">{fmtPrice(decision.stop_loss)}</span>
            <span className="text-gray-600 ml-1 font-sans text-[10px]">(LLM thesis boundary)</span>
          </span>
          <span className="text-gray-500">
            Underlying Target{' '}
            <span className="text-emerald-400">{fmtPrice(decision.target)}</span>
            <span className="text-gray-600 ml-1 font-sans text-[10px]">(LLM thesis boundary)</span>
          </span>
          {trade.exit_price != null && (
            <span className="text-gray-500">Option exit <span className={trade.pnl != null && trade.pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}>{fmtPrice(trade.exit_price)}</span></span>
          )}
        </div>
      )}

      {/* Indicators grid */}
      {ind && (
        <div className="grid grid-cols-4 gap-x-4 gap-y-1 font-mono text-gray-400">
          <span>Price <span className="text-gray-200">{fmtPrice(ind.price)}</span></span>
          <span>RSI <span className={ind.rsi > 70 ? 'text-red-400' : ind.rsi < 30 ? 'text-emerald-400' : 'text-gray-200'}>{fmt(ind.rsi, 1)}</span></span>
          <span>VWAP <span className="text-gray-200">{fmtPrice(ind.vwap)}</span></span>
          <span>MACD <span className={ind.macd_signal === 'BULLISH' ? 'text-emerald-400' : ind.macd_signal === 'BEARISH' ? 'text-red-400' : 'text-gray-400'}>{ind.macd_signal ?? '—'}</span></span>
          <span>EMA9 <span className="text-gray-200">{fmtPrice(ind.ema_9)}</span></span>
          <span>EMA21 <span className="text-gray-200">{fmtPrice(ind.ema_21)}</span></span>
          <span>CPR <span className={ind.cpr_signal === 'ABOVE_CPR' ? 'text-emerald-400' : ind.cpr_signal === 'BELOW_CPR' ? 'text-red-400' : 'text-yellow-400'}>{ind.cpr_signal?.replace('_CPR', '') ?? '—'}</span></span>
          <span>Day <span className="text-gray-200">{ind.day_type ?? '—'}</span></span>
          <span>D-High <span className="text-emerald-400/80">{fmtPrice(ind.day_high)}</span></span>
          <span>D-Low <span className="text-red-400/80">{fmtPrice(ind.day_low)}</span></span>
          <span>Breakout <span className={ind.range_breakout !== 'NONE' ? 'text-yellow-400' : 'text-gray-500'}>{ind.range_breakout ?? '—'}</span></span>
          <span>Sentiment <span className="text-gray-200">{fmt(ind.sentiment_score)}</span></span>
        </div>
      )}

      {/* Reasoning */}
      {(trade.reasoning || decision?.reasoning) && (
        <div className="border-t border-gray-700/50 pt-2">
          <div className="text-gray-500 mb-0.5">Reasoning</div>
          <div className="text-gray-300 leading-relaxed">
            {trade.reasoning || decision?.reasoning}
          </div>
        </div>
      )}

      {!decision && !trade.reasoning && (
        <div className="text-gray-600">No decision context available for this trade.</div>
      )}
    </div>
  )
}

function StatusBadge({ status, exitReason }: { status: string; exitReason?: string }) {
  const label = exitReason ?? status
  const color =
    status === 'OPEN'
      ? 'bg-blue-900/50 text-blue-300'
      : label === 'TRAIL_STOP' || label === 'CLOSED'
        ? 'bg-emerald-900/50 text-emerald-300'
        : label === 'MANUAL_UI_EXIT'
          ? 'bg-violet-900/50 text-violet-300'
          : label === 'USER_EXIT_FYERS'
            ? 'bg-amber-900/50 text-amber-300'
            : 'bg-red-900/50 text-red-300'

  return (
    <span className={`inline-block px-1.5 py-0.5 rounded text-xs font-medium ${color}`}>
      {label}
    </span>
  )
}
