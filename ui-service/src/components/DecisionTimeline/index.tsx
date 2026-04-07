import type { Decision } from '../../types'
import { parseDate } from '../../utils/date'

interface Props {
  decisions: Decision[]
}

function DecisionDot({ decision }: { decision: string }) {
  const cls =
    decision === 'BUY'  ? 'bg-emerald-500' :
    decision === 'SELL' ? 'bg-red-500' :
                          'bg-gray-500'
  return <span className={`inline-block w-2 h-2 rounded-full ${cls}`} />
}

function ConfidenceBar({ confidence }: { confidence: number }) {
  const pct = Math.round(confidence * 100)
  const color = pct >= 75 ? 'bg-emerald-500' : pct >= 60 ? 'bg-yellow-500' : 'bg-gray-600'
  return (
    <div className="flex items-center gap-1">
      <div className="w-16 h-1 bg-gray-700 rounded overflow-hidden">
        <div className={`h-full ${color} rounded`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs text-gray-500">{pct}%</span>
    </div>
  )
}

export function DecisionTimeline({ decisions }: Props) {
  if (!decisions.length) {
    return (
      <div className="text-xs text-gray-500 p-4">
        No decision history yet.
      </div>
    )
  }

  return (
    <div className="space-y-1 max-h-80 overflow-y-auto">
      {decisions.map((d) => {
        const ts = parseDate(d.timestamp)
        const timeStr = ts.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: false })
        const dateStr = ts.toLocaleDateString('en-IN', { day: '2-digit', month: 'short' })
        return (
          <div
            key={d.decision_id}
            className="flex items-start gap-2 px-3 py-2 rounded hover:bg-gray-800 transition-colors"
          >
            <div className="flex flex-col items-center gap-0.5 pt-1 shrink-0">
              <DecisionDot decision={d.decision} />
              <div className="w-px h-full bg-gray-800 flex-1" />
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <span className={`text-xs font-mono font-semibold ${
                  d.decision === 'BUY' ? 'text-emerald-400' :
                  d.decision === 'SELL' ? 'text-red-400' :
                  'text-gray-400'
                }`}>
                  {d.decision}
                </span>
                <span className="text-xs text-gray-500 font-mono">
                  {dateStr} {timeStr}
                </span>
                {d.acted_upon && (
                  <span className="text-xs bg-blue-500/20 text-blue-400 border border-blue-500/30 rounded px-1">
                    ACTED
                  </span>
                )}
              </div>
              <ConfidenceBar confidence={d.confidence} />
              <p className="text-xs text-gray-400 mt-0.5 truncate" title={d.reasoning}>
                {d.reasoning}
              </p>
            </div>
          </div>
        )
      })}
    </div>
  )
}
