import type { ReactNode } from 'react'
import type { ContextSnapshot, OptionsChain } from '../../types'

interface Props {
  context: ContextSnapshot | null
  loading?: boolean
  optionsChain?: OptionsChain | null
}

// ── Tooltip ─────────────────────────────────────────────────────────────────

function Tooltip({ tip, children }: { tip: string; children: ReactNode }) {
  return (
    <span className="relative group inline-flex items-center gap-0.5 cursor-help">
      {children}
      <span className="text-gray-600 text-[10px] leading-none">ⓘ</span>
      <span className="
        absolute bottom-full left-0 mb-1.5 w-52
        text-[11px] leading-snug text-gray-200
        bg-gray-800 border border-gray-600 rounded px-2 py-1.5
        opacity-0 group-hover:opacity-100 transition-opacity duration-150
        pointer-events-none z-50 shadow-xl whitespace-normal
      ">
        {tip}
      </span>
    </span>
  )
}

function Label({ children, tip }: { children: ReactNode; tip: string }) {
  return (
    <Tooltip tip={tip}>
      <span className="text-xs text-gray-500">{children}</span>
    </Tooltip>
  )
}

function SectionHeader({ children }: { children: ReactNode }) {
  return (
    <div className="text-[10px] font-semibold text-gray-500 uppercase tracking-widest pt-1 pb-0.5 border-t border-gray-800 mt-1">
      {children}
    </div>
  )
}

// ── Badges ───────────────────────────────────────────────────────────────────

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

// ── PCR badge ────────────────────────────────────────────────────────────────

function PcrBadge({ pcr }: { pcr: number }) {
  const label = pcr > 1.2 ? 'BULLISH BIAS' : pcr < 0.8 ? 'BEARISH BIAS' : 'NEUTRAL'
  const color =
    pcr > 1.2 ? 'text-emerald-400 bg-emerald-400/10 border-emerald-400/30' :
    pcr < 0.8 ? 'text-red-400 bg-red-400/10 border-red-400/30' :
    'text-gray-400 bg-gray-400/10 border-gray-400/30'
  return (
    <span className={`text-[10px] font-mono px-1 py-0.5 rounded border ${color}`}>
      {label}
    </span>
  )
}

function VixBadge({ vix }: { vix: number }) {
  const label = vix > 20 ? 'HIGH' : vix < 15 ? 'LOW' : 'MODERATE'
  const color =
    vix > 20 ? 'text-red-400 bg-red-400/10 border-red-400/30' :
    vix < 15 ? 'text-emerald-400 bg-emerald-400/10 border-emerald-400/30' :
    'text-yellow-400 bg-yellow-400/10 border-yellow-400/30'
  return (
    <span className={`text-[10px] font-mono px-1 py-0.5 rounded border ${color}`}>
      {label}
    </span>
  )
}

// ── Metric row ───────────────────────────────────────────────────────────────

function MetricRow({
  label, tip, value, valueClass = 'text-gray-200',
}: {
  label: string
  tip: string
  value: ReactNode
  valueClass?: string
}) {
  return (
    <div className="flex items-center justify-between gap-2 py-0.5">
      <Label tip={tip}>{label}</Label>
      <span className={`font-mono text-xs font-medium ${valueClass}`}>{value}</span>
    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────────

export function ContextPanel({ context, loading, optionsChain }: Props) {
  if (loading) {
    return (
      <div className="bg-gray-900 rounded-lg border border-gray-800 p-4 text-sm text-gray-500 animate-pulse">
        Loading historical context…
      </div>
    )
  }

  if (!context && !optionsChain) {
    return (
      <div className="bg-gray-900 rounded-lg border border-gray-800 p-4 text-sm text-gray-500">
        No historical context — data accumulates after first trading day.
      </div>
    )
  }

  const {
    previous_day, today_cpr, multi_timeframe_trend,
    volatility, news_sentiment, key_levels, intraday_range,
  } = context ?? {}

  return (
    <div className="bg-gray-900 rounded-lg border border-gray-800 p-4 text-sm">
      <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-3">
        Historical Context
      </h3>

      {/* ── Previous Day ────────────────────────────────────────── */}
      {previous_day?.close && (
        <>
          <SectionHeader>Previous Day</SectionHeader>
          <div className="grid grid-cols-3 gap-1.5 mt-1.5 mb-2">
            {[
              {
                label: 'Prev High', value: `₹${Number(previous_day.high).toLocaleString('en-IN')}`,
                color: 'text-emerald-400',
                tip: "Yesterday's intraday high. Acts as primary resistance — a break above signals bullish momentum and often triggers stop-loss driven buying.",
              },
              {
                label: 'Prev Low', value: `₹${Number(previous_day.low).toLocaleString('en-IN')}`,
                color: 'text-red-400',
                tip: "Yesterday's intraday low. Acts as primary support — a break below signals bearish momentum and often triggers stop-loss driven selling.",
              },
              {
                label: 'Prev Close', value: `₹${Number(previous_day.close).toLocaleString('en-IN')}`,
                color: 'text-gray-200',
                tip: "Yesterday's closing price. Used to compute today's CPR, Pivot, and R/S levels. An opening gap relative to prev close signals overnight sentiment shift.",
              },
            ].map(({ label, value, color, tip }) => (
              <div key={label} className="bg-gray-800 rounded p-2">
                <Label tip={tip}>{label}</Label>
                <div className={`font-mono text-xs font-semibold mt-0.5 ${color}`}>{value}</div>
              </div>
            ))}
          </div>
        </>
      )}

      {/* ── CPR ─────────────────────────────────────────────────── */}
      {today_cpr?.pivot && (
        <>
          <SectionHeader>Today's CPR</SectionHeader>
          <div className="mt-1.5 mb-2 space-y-1">
            <div className="flex justify-between items-center">
              <div className="flex gap-3 font-mono text-xs">
                <Tooltip tip="Bottom Central Pivot Range. When price is above BC, bulls have intraday control. Price re-entering CPR from above is a short signal.">
                  <span className="text-gray-500 mr-0.5">BC</span>
                  <span className="text-blue-400">₹{Number(today_cpr.bc).toLocaleString('en-IN')}</span>
                </Tooltip>
                <Tooltip tip="Daily pivot point — arithmetic mean of prev H/L/C. Key equilibrium level. Price action above pivot = bullish bias; below = bearish bias.">
                  <span className="text-gray-500 mr-0.5">P</span>
                  <span className="text-orange-400">₹{Number(today_cpr.pivot).toLocaleString('en-IN')}</span>
                </Tooltip>
                <Tooltip tip="Top Central Pivot Range. When price is below TC, bears have intraday control. Price re-entering CPR from below is a long signal.">
                  <span className="text-gray-500 mr-0.5">TC</span>
                  <span className="text-blue-400">₹{Number(today_cpr.tc).toLocaleString('en-IN')}</span>
                </Tooltip>
              </div>
            </div>
            <div className="flex items-center gap-1.5">
              <Label tip="Width of the CPR band as % of price. Narrow (<0.25%) = trending day expected — price will pick a direction and run. Wide (>0.5%) = rangebound day expected — fade the extremes.">
                Width
              </Label>
              <span className="font-mono text-xs text-gray-300">{today_cpr.cpr_width_pct?.toFixed(2)}%</span>
              <span className="text-xs text-gray-500">·</span>
              <span className="text-xs text-gray-400">{today_cpr.cpr_type}</span>
            </div>
          </div>
        </>
      )}

      {/* ── Multi-Timeframe Trend ────────────────────────────────── */}
      {multi_timeframe_trend && (
        <>
          <SectionHeader>Multi-Timeframe Trend</SectionHeader>
          <div className="flex gap-3 mt-1.5 mb-2 flex-wrap">
            {([
              ['15m', "15-minute trend from EMA9/21 crossover + candle structure. Short-term bias — tells you what's happening right now."],
              ['1h',  "1-hour trend. Acts as the intraday session filter — trade only in the direction of this trend unless a strong reversal signal fires."],
              ['daily', "Daily trend from the prior 20 sessions. Provides macro bias — highest weight in the LLM decision rules. Conflicting signals reduce confidence."],
            ] as [string, string][]).map(([tf, tip]) => (
              <Tooltip key={tf} tip={tip}>
                <div className="flex items-center gap-1">
                  <span className="text-xs text-gray-500">{tf}</span>
                  <TrendBadge trend={(multi_timeframe_trend as any)[tf]} />
                </div>
              </Tooltip>
            ))}
          </div>
        </>
      )}

      {/* ── Key S/R Zones ────────────────────────────────────────── */}
      {key_levels && (
        <>
          <SectionHeader>Historical S/R Zones</SectionHeader>
          <div className="grid grid-cols-2 gap-2 mt-1.5 mb-2">
            <div>
              <Label tip="Multi-year swing high zones where selling was historically concentrated. Computed from 5yr daily chart. The more tests a zone has, the stronger it is.">
                Resistance
              </Label>
              {(key_levels.resistance_zones ?? []).slice(0, 3).map((z, i) => (
                <div key={i} className="text-xs font-mono text-red-400 mt-0.5">
                  ₹{Number(z).toLocaleString('en-IN')}
                </div>
              ))}
            </div>
            <div>
              <Label tip="Multi-year swing low zones where buying was historically concentrated. Computed from 5yr daily chart. Price tends to bounce at these levels; breaks lead to extended moves.">
                Support
              </Label>
              {(key_levels.support_zones ?? []).slice(0, 3).map((z, i) => (
                <div key={i} className="text-xs font-mono text-emerald-400 mt-0.5">
                  ₹{Number(z).toLocaleString('en-IN')}
                </div>
              ))}
            </div>
          </div>
        </>
      )}

      {/* ── Intraday Range ───────────────────────────────────────── */}
      {intraday_range && (
        <>
          <SectionHeader>Intraday Range</SectionHeader>
          <div className="mt-1.5 mb-2">
            <Label tip="Today's intraday high and low so far. Used to compute day range % and identify intraday breakout levels. A narrow range with low ATR signals a potential explosive breakout.">
              Today
            </Label>
            <span className="font-mono text-xs ml-1.5">
              <span className="text-emerald-400">₹{Number(intraday_range.high).toLocaleString('en-IN')}</span>
              <span className="text-gray-600 mx-1">—</span>
              <span className="text-red-400">₹{Number(intraday_range.low).toLocaleString('en-IN')}</span>
            </span>
          </div>
        </>
      )}

      {/* ── Volatility ───────────────────────────────────────────── */}
      {volatility && (
        <>
          <SectionHeader>Volatility</SectionHeader>
          <div className="mt-1.5 mb-2 space-y-1">
            <MetricRow
              label="15m ATR"
              tip="Average True Range on the 15-minute chart as % of price. Higher = wider expected moves within the session. Use to calibrate intraday stop-loss width."
              value={`${volatility['15m_atr_pct']?.toFixed(2)}%`}
            />
            <MetricRow
              label="Daily ATR"
              tip="Average True Range on the daily chart as % of price. Represents the 'normal' daily swing. Used to set targets — a 1:1.5 R:R stop should be well within daily ATR."
              value={`${volatility.daily_atr_pct?.toFixed(2)}%`}
            />
          </div>
        </>
      )}

      {/* ── Options Market Structure ──────────────────────────────── */}
      <>
        <SectionHeader>Options Market Structure</SectionHeader>
        {!optionsChain ? (
          <div className="text-xs text-gray-600 mt-1.5 mb-2">
            Awaiting first 5-min scan after market open.
          </div>
        ) : (
          <div className="mt-1.5 mb-2 space-y-1">
            <MetricRow
              label="VIX"
              tip="India VIX — the market's implied volatility index. >20 = high uncertainty, widen stop-loss to 0.5–0.7%. <15 = calm market, tighten stops. VIX spikes during selloffs; falling VIX confirms bullish recoveries."
              value={
                <span className="flex items-center gap-1.5">
                  {optionsChain.vix.toFixed(2)}
                  <VixBadge vix={optionsChain.vix} />
                  <span className={`text-[10px] ${optionsChain.vix_change_pct >= 0 ? 'text-red-400' : 'text-emerald-400'}`}>
                    {optionsChain.vix_change_pct >= 0 ? '+' : ''}{optionsChain.vix_change_pct?.toFixed(2)}%
                  </span>
                </span>
              }
            />
            <MetricRow
              label="PCR"
              tip="Put-Call Ratio = Total Put OI ÷ Total Call OI. >1.2 = contrarian bullish (panic put buying — market makers are net short puts and will defend). <0.8 = contrarian bearish (retail buying calls aggressively — market makers are net short calls above). PCR is a sentiment extremes indicator, not a trend follower."
              value={
                <span className="flex items-center gap-1.5">
                  {optionsChain.pcr.toFixed(3)}
                  <PcrBadge pcr={optionsChain.pcr} />
                </span>
              }
            />
            <MetricRow
              label="Call Wall"
              tip="Strike with highest call open interest — institutions have sold calls here, creating resistance. Delta hedging by these writers creates selling pressure as price approaches. A clean break above the call wall is a very strong signal."
              value={`₹${Number(optionsChain.call_wall).toLocaleString('en-IN')}`}
              valueClass="text-red-400"
            />
            <MetricRow
              label="Call Wall OI"
              tip="Open interest at the call wall strike. Higher OI = stronger resistance ceiling. This is the notional number of contracts that will need to be hedged if price rallies toward this level."
              value={(optionsChain.call_wall_oi / 1_000_000).toFixed(1) + 'M'}
              valueClass="text-red-400/70"
            />
            <MetricRow
              label="Put Wall"
              tip="Strike with highest put open interest — institutions have sold puts here, creating support. These writers will buy futures/spot to hedge delta as price falls, creating a floor. A break below the put wall signals institutional capitulation."
              value={`₹${Number(optionsChain.put_wall).toLocaleString('en-IN')}`}
              valueClass="text-emerald-400"
            />
            <MetricRow
              label="Put Wall OI"
              tip="Open interest at the put wall strike. Higher OI = stronger support floor. Tracks the aggregate notional hedge that market makers must maintain."
              value={(optionsChain.put_wall_oi / 1_000_000).toFixed(1) + 'M'}
              valueClass="text-emerald-400/70"
            />
            <MetricRow
              label="Max Pain"
              tip="The strike at which total option buyer losses are maximised (and option writer profits are maximised). Price gravitates toward max pain into expiry (especially on expiry day afternoon) as market makers delta-hedge. Strong magnet on Thursday."
              value={`₹${Number(optionsChain.max_pain).toLocaleString('en-IN')}`}
              valueClass="text-yellow-400"
            />
            <MetricRow
              label="Futures"
              tip="Near-month futures price. Used to compute the basis (futures − spot). Positive basis = contango = institutional long bias. Negative = backwardation = bearish or hedging pressure."
              value={`₹${Number(optionsChain.futures).toLocaleString('en-IN')}`}
            />
            <MetricRow
              label="Basis"
              tip="Futures price minus spot price. Positive (contango) = institutional net long — bullish signal. Negative (backwardation) = net short or heavy hedging — bearish signal. Basis > 0.1% of spot = meaningful directional signal."
              value={`${optionsChain.basis >= 0 ? '+' : ''}${optionsChain.basis.toFixed(1)}`}
              valueClass={optionsChain.basis > 0 ? 'text-emerald-400' : optionsChain.basis < 0 ? 'text-red-400' : 'text-gray-400'}
            />
            <div className="flex items-center justify-between py-0.5">
              <Label tip="The weekly options expiry date for which this chain data was fetched. OI data resets each expiry. Max pain effect is strongest on expiry day.">
                Expiry
              </Label>
              <span className="font-mono text-xs text-gray-400">{optionsChain.expiry}</span>
            </div>
          </div>
        )}
      </>

      {/* ── News Sentiment ────────────────────────────────────────── */}
      {news_sentiment && (
        <>
          <SectionHeader>News Sentiment</SectionHeader>
          <div className="mt-1.5">
            <div className="flex items-center gap-2 mb-1">
              <Tooltip tip="Aggregate sentiment score averaged across all recent headlines. Range: −1 (very bearish) to +1 (very bullish). Scores are computed using keyword/NLP analysis on title + summary.">
                <span className="text-xs text-gray-500">{news_sentiment.count} items · score {news_sentiment.avg_score?.toFixed(2)}</span>
              </Tooltip>
              <SentimentBadge label={news_sentiment.label} />
            </div>
            {news_sentiment.headlines?.slice(0, 2).map((h, i) => (
              <div key={i} className="text-xs text-gray-600 mt-1 truncate" title={h.title}>
                {h.title}
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  )
}
