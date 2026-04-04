import { useEffect, useRef } from 'react'
import { format } from 'date-fns'
import { Badge } from '../shared/Badge'
import type { Decision } from '../../types'

interface Props {
  decisions: Decision[]
  sseConnected: boolean
}

export function DecisionFeed({ decisions, sseConnected }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null)

  return (
    <div className="bg-gray-900 rounded-lg border border-gray-800 flex flex-col h-full">
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-800">
        <h2 className="text-sm font-semibold text-gray-400 uppercase tracking-wide">Decision Feed</h2>
        <div className="flex items-center gap-2">
          <div className={`w-2 h-2 rounded-full ${sseConnected ? 'bg-emerald-400 animate-pulse' : 'bg-gray-600'}`} />
          <span className="text-xs text-gray-500">{sseConnected ? 'Live' : 'Reconnecting...'}</span>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto p-3 space-y-2 max-h-96">
        {decisions.length === 0 && (
          <div className="text-center text-gray-600 text-sm py-8">
            Waiting for decisions...
          </div>
        )}
        {decisions.map((d) => (
          <DecisionCard key={d.decision_id} decision={d} />
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}

function DecisionCard({ decision: d }: { decision: Decision }) {
  const decisionVariant = d.decision === 'BUY' ? 'buy' : d.decision === 'SELL' ? 'sell' : 'hold'

  return (
    <div className="bg-gray-800/60 rounded-lg p-3 border border-gray-700/50 hover:border-gray-600 transition-colors">
      <div className="flex items-start justify-between gap-2 mb-2">
        <div className="flex items-center gap-2 flex-wrap">
          <Badge label={d.decision} variant={decisionVariant} size="md" />
          <span className="text-sm font-mono text-blue-400">{d.symbol.replace('NSE:', '')}</span>
          <span className="text-xs text-gray-500 font-mono">
            {d.indicators_snapshot?.price ? `₹${d.indicators_snapshot.price.toLocaleString('en-IN')}` : ''}
          </span>
        </div>
        <div className="text-right shrink-0">
          <div className="text-xs text-gray-500">
            {format(new Date(d.timestamp), 'HH:mm:ss')}
          </div>
          <div className="text-xs font-mono text-gray-400">
            {(d.confidence * 100).toFixed(0)}% conf
          </div>
        </div>
      </div>

      {d.option_symbol && (
        <div className="flex items-center gap-2 mb-2 px-2 py-1 bg-gray-700/40 rounded text-xs font-mono">
          <span className="text-gray-500">Option:</span>
          <span className="text-amber-400">{d.option_symbol.replace('NSE:', '')}</span>
          {d.option_strike && (
            <>
              <span className="text-gray-600">·</span>
              <span className="text-gray-300">Strike ₹{d.option_strike.toLocaleString('en-IN')}</span>
              {d.option_type && (
                <span className={`font-semibold ${d.option_type === 'CE' ? 'text-emerald-400' : 'text-red-400'}`}>
                  {d.option_type}
                </span>
              )}
            </>
          )}
          {d.option_price && (
            <>
              <span className="text-gray-600">·</span>
              <span className="text-gray-300">@ ₹{d.option_price.toFixed(2)}</span>
            </>
          )}
          {d.option_expiry && (
            <>
              <span className="text-gray-600">·</span>
              <span className="text-gray-500">exp {d.option_expiry}</span>
            </>
          )}
        </div>
      )}

      <p className="text-sm text-gray-300 leading-relaxed mb-2">{d.reasoning}</p>

      <div className="grid grid-cols-3 gap-2 text-xs text-gray-500">
        <span>CPR: <span className="text-gray-300">{d.indicators_snapshot?.cpr_signal?.replace('_CPR', '') ?? '-'}</span></span>
        <span>RSI: <span className="text-gray-300">{d.indicators_snapshot?.rsi?.toFixed(1) ?? '-'}</span></span>
        <span>R/R: <span className="text-gray-300">{d.risk_reward?.toFixed(1) ?? '-'}x</span></span>
        <span>SL: <span className="text-red-400">₹{d.stop_loss?.toLocaleString('en-IN')}</span></span>
        <span>Target: <span className="text-emerald-400">₹{d.target?.toLocaleString('en-IN')}</span></span>
        <span>Sent: <span className={d.indicators_snapshot?.sentiment_score >= 0 ? 'text-emerald-400' : 'text-red-400'}>
          {d.indicators_snapshot?.sentiment_score >= 0 ? '+' : ''}{d.indicators_snapshot?.sentiment_score?.toFixed(2) ?? '0'}
        </span></span>
      </div>
    </div>
  )
}
