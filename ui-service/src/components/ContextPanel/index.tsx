import type { ContextSnapshot } from '../../types'

interface Props {
  context: ContextSnapshot | null
  loading?: boolean
}

function TrendBadge({ trend }: { trend: string }) {
  const color =
    trend === 'BULLISH' ? 'text-emerald-400 bg-emerald-400/10 border-emerald-400/30' :
    trend === 'BEARISH' ? 'text-red-400 bg-red-400/10 border-red-400/30' :
    'text-gray-400 bg-gray-400/10 border-gray-400/30'
  return (
    <span className={`text-xs font-mono px-1.5 py-0.5 rounded border ${color}`}>
      {trend ?? '—'}
    </span>
  )
}

function SentimentBadge({ label }: { label: string }) {
  const color =
    label === 'BULLISH' ? 'text-emerald-400' :
    label === 'BEARISH' ? 'text-red-400' :
    'text-yellow-400'
  return <span className={`font-mono font-semibold ${color}`}>{label}</span>
}

export function ContextPanel({ context, loading }: Props) {
  if (loading) {
    return (
      <div className="bg-gray-900 rounded-lg border border-gray-800 p-4 text-sm text-gray-500 animate-pulse">
        Loading historical context…
      </div>
    )
  }

  if (!context) {
    return (
      <div className="bg-gray-900 rounded-lg border border-gray-800 p-4 text-sm text-gray-500">
        No historical context — data accumulates after first trading day.
      </div>
    )
  }

  const { previous_day, today_cpr, multi_timeframe_trend, volatility, news_sentiment, key_levels, intraday_range } = context

  return (
    <div className="bg-gray-900 rounded-lg border border-gray-800 p-4 space-y-3 text-sm">
      <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider">
        Historical Context
      </h3>

      {/* Previous Day */}
      {previous_day?.close && (
        <div className="grid grid-cols-3 gap-2">
          {[
            { label: 'Prev High', value: `₹${Number(previous_day.high).toLocaleString('en-IN')}`, color: 'text-emerald-400' },
            { label: 'Prev Low',  value: `₹${Number(previous_day.low).toLocaleString('en-IN')}`, color: 'text-red-400' },
            { label: 'Prev Close', value: `₹${Number(previous_day.close).toLocaleString('en-IN')}`, color: 'text-gray-200' },
          ].map(({ label, value, color }) => (
            <div key={label} className="bg-gray-800 rounded p-2">
              <div className="text-xs text-gray-500">{label}</div>
              <div className={`font-mono text-xs font-semibold ${color}`}>{value}</div>
            </div>
          ))}
        </div>
      )}

      {/* CPR */}
      {today_cpr?.pivot && (
        <div>
          <div className="text-xs text-gray-500 mb-1">Today's CPR</div>
          <div className="flex gap-3 font-mono text-xs">
            <span className="text-blue-400">BC ₹{Number(today_cpr.bc).toLocaleString('en-IN')}</span>
            <span className="text-orange-400">P ₹{Number(today_cpr.pivot).toLocaleString('en-IN')}</span>
            <span className="text-blue-400">TC ₹{Number(today_cpr.tc).toLocaleString('en-IN')}</span>
          </div>
          <div className="text-xs text-gray-500 mt-0.5">{today_cpr.cpr_type}</div>
        </div>
      )}

      {/* Multi-Timeframe Trend */}
      <div>
        <div className="text-xs text-gray-500 mb-1">Trend</div>
        <div className="flex gap-2 flex-wrap">
          {Object.entries(multi_timeframe_trend).map(([tf, trend]) => (
            <div key={tf} className="flex items-center gap-1">
              <span className="text-xs text-gray-500">{tf}</span>
              <TrendBadge trend={trend} />
            </div>
          ))}
        </div>
      </div>

      {/* Key S/R Zones */}
      {key_levels && (
        <div className="grid grid-cols-2 gap-2">
          <div>
            <div className="text-xs text-gray-500 mb-1">Resistance</div>
            {(key_levels.resistance_zones ?? []).slice(0, 3).map((z, i) => (
              <div key={i} className="text-xs font-mono text-red-400">
                ₹{Number(z).toLocaleString('en-IN')}
              </div>
            ))}
          </div>
          <div>
            <div className="text-xs text-gray-500 mb-1">Support</div>
            {(key_levels.support_zones ?? []).slice(0, 3).map((z, i) => (
              <div key={i} className="text-xs font-mono text-emerald-400">
                ₹{Number(z).toLocaleString('en-IN')}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Intraday Range */}
      {intraday_range && (
        <div className="text-xs text-gray-500">
          Today range:{' '}
          <span className="text-emerald-400 font-mono">₹{Number(intraday_range.high).toLocaleString('en-IN')}</span>
          {' — '}
          <span className="text-red-400 font-mono">₹{Number(intraday_range.low).toLocaleString('en-IN')}</span>
        </div>
      )}

      {/* Volatility */}
      <div className="text-xs text-gray-500">
        Volatility — 15m ATR:{' '}
        <span className="text-gray-300 font-mono">{volatility?.['15m_atr_pct']?.toFixed(2)}%</span>
        {' '}| Daily ATR:{' '}
        <span className="text-gray-300 font-mono">{volatility?.daily_atr_pct?.toFixed(2)}%</span>
      </div>

      {/* News Sentiment */}
      {news_sentiment && (
        <div className="border-t border-gray-800 pt-2">
          <div className="text-xs text-gray-500 mb-1">
            News 24h ({news_sentiment.count} items)
          </div>
          <SentimentBadge label={news_sentiment.label} />
          <span className="text-xs text-gray-500 ml-1">
            score: {news_sentiment.avg_score?.toFixed(2)}
          </span>
          {news_sentiment.headlines?.slice(0, 2).map((h, i) => (
            <div key={i} className="text-xs text-gray-500 mt-1 truncate" title={h.title}>
              {h.title}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
