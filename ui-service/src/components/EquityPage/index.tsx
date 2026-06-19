import { useCallback, useEffect, useRef, useState } from 'react'
import {
  fetchEquityPositions, fetchEquityAnalysisCached, refreshEquityAnalysis,
  getEquityMode, setEquityMode, equityTrade,
  type EquityCandidate, type EquityPosition, type EquityRec,
} from '../../api/equity'

const fmt = (n?: number) =>
  n == null ? '–' : Number(n).toLocaleString('en-IN', { maximumFractionDigits: 2 })

function pillClass(action?: string) {
  const a = (action || '').toUpperCase()
  if (['ENTER', 'HOLD', 'ADD'].includes(a)) return 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30'
  if (['EXIT', 'AVOID'].includes(a)) return 'bg-red-500/15 text-red-400 border-red-500/30'
  if (['WATCH', 'TRIM'].includes(a)) return 'bg-amber-500/15 text-amber-400 border-amber-500/30'
  return 'bg-gray-700/40 text-gray-400 border-gray-600/40'
}

function Pill({ rec }: { rec?: EquityRec }) {
  if (!rec?.action) return null
  return (
    <span className={`px-2 py-0.5 rounded text-xs font-bold border ${pillClass(rec.action)}`}>
      {rec.action}{rec.conviction ? <span className="font-normal opacity-70"> · {rec.conviction}</span> : null}
    </span>
  )
}

function Reason({ text }: { text?: string }) {
  if (!text) return null
  return (
    <div className="mt-2 px-3 py-2 bg-gray-950 border border-gray-800 rounded text-[13px] text-gray-300">
      <span className="text-[10px] font-bold tracking-wider text-gray-500 mr-1">WHY</span>{text}
    </div>
  )
}

export function EquityPage() {
  const [mode, setMode] = useState('simulation')
  const [positions, setPositions] = useState<EquityPosition[]>([])
  const [candidates, setCandidates] = useState<EquityCandidate[]>([])
  const [advice, setAdvice] = useState<Record<string, EquityRec>>({})
  const [updated, setUpdated] = useState<string | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const pollRef = useRef<number | null>(null)

  const loadPositions = useCallback(() => {
    fetchEquityPositions().then((d) => setPositions(d.positions || [])).catch(() => {})
  }, [])

  const applyReport = useCallback((report: any) => {
    if (!report) { setUpdated(null); return }
    setUpdated(report.generated_at ?? null)
    const m: Record<string, EquityRec> = {}
    ;(report.holdings || []).forEach((c: any) => { m[c.symbol] = c.recommendation || {} })
    setAdvice(m)
    setCandidates(report.candidates || [])
  }, [])

  const loadCached = useCallback(() => {
    return fetchEquityAnalysisCached().then((d) => applyReport(d.report)).catch(() => {})
  }, [applyReport])

  useEffect(() => {
    getEquityMode().then((d) => setMode(d.mode)).catch(() => {})
    loadCached()
    loadPositions()
    return () => { if (pollRef.current) window.clearInterval(pollRef.current) }
  }, [loadCached, loadPositions])

  const toggleMode = async () => {
    const next = mode === 'live' ? 'simulation' : 'live'
    if (next === 'live' && !window.confirm('Switch to LIVE? Buy/Sell will place REAL Fyers orders.')) return
    const d = await setEquityMode(next)
    setMode(d.mode)
  }

  const refresh = async () => {
    const prev = updated
    setRefreshing(true)
    await refreshEquityAnalysis(8).catch(() => {})
    let waited = 0
    if (pollRef.current) window.clearInterval(pollRef.current)
    pollRef.current = window.setInterval(async () => {
      waited += 8
      const d = await fetchEquityAnalysisCached().catch(() => null)
      const r = d?.report
      if (r && r.generated_at !== prev) {
        window.clearInterval(pollRef.current!); applyReport(r); loadPositions(); setRefreshing(false)
      } else if (waited > 360) {
        window.clearInterval(pollRef.current!); setRefreshing(false)
      }
    }, 8000)
  }

  const trade = async (symbol: string, side: string) => {
    const name = symbol.split(':')[1]?.replace('-EQ', '') ?? symbol
    const qty = Number(window.prompt(`${side} how many shares of ${name}?`, '1'))
    if (!qty || qty <= 0) return
    let d = await equityTrade(symbol, side, qty, false)
    if (d.status === 'confirm_required') {
      if (!window.confirm(d.message)) return
      d = await equityTrade(symbol, side, qty, true)
    }
    window.alert(`${d.action || d.status} ${d.mode || ''} ${name}` +
      (d.fill ? ` @ ₹${d.fill}` : '') +
      (d.realized_pnl != null ? ` · P&L ₹${d.realized_pnl}` : '') +
      (d.detail ? ` — ${d.detail}` : ''))
    loadPositions()
  }

  return (
    <div className="p-4">
      <div className="flex items-center gap-3 mb-4">
        <h2 className="text-lg font-bold text-white">Equity Advisor</h2>
        <span className={`px-2.5 py-0.5 rounded-full text-xs font-bold border ${
          mode === 'live' ? 'bg-red-500/10 text-red-400 border-red-500/30'
                          : 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30'}`}>
          {mode.toUpperCase()}
        </span>
        <button onClick={toggleMode} className="text-xs px-2 py-1 rounded border border-gray-700 text-gray-300 hover:bg-gray-800">
          switch
        </button>
        <span className="flex-1" />
        <span className="text-xs text-gray-500">
          {updated ? `updated ${new Date(updated).toLocaleString('en-IN')}` : 'no analysis yet'}
        </span>
        <button onClick={refresh} disabled={refreshing}
          className="text-sm px-3 py-1.5 rounded bg-blue-600 text-white font-semibold disabled:opacity-60">
          {refreshing ? 'Analysing…' : '↻ Refresh recommendations'}
        </button>
      </div>

      <div className="grid grid-cols-2 gap-5 max-[980px]:grid-cols-1">
        {/* Potential stocks */}
        <div>
          <h3 className="text-xs font-bold tracking-wider uppercase text-gray-500 mb-3">Potential stocks — entry advice</h3>
          {candidates.length === 0 ? (
            <div className="text-center text-sm text-gray-500 border border-dashed border-gray-800 rounded-lg p-6">
              No recommendations yet. Click <b>↻ Refresh recommendations</b> (≈1–2 min).
            </div>
          ) : candidates.map((c) => {
            const a = c.recommendation || {}
            return (
              <div key={c.symbol} className="bg-gray-900 border border-gray-800 rounded-xl p-4 mb-3">
                <div className="flex items-center gap-2">
                  <span className="font-bold text-base text-white">{c.name}</span>
                  <span className="text-xs text-gray-500">₹{fmt(c.ltp)} · {c.regime} · mom {fmt(c.momentum_12m_pct)}%</span>
                  <span className="flex-1" /><Pill rec={a} />
                </div>
                <div className="mt-2 text-xs text-gray-500">
                  entry <b className="text-gray-200">₹{fmt((a.entry_zone && a.entry_zone[0]) ?? c.ltp)}</b> ·
                  stop <b className="text-gray-200">₹{fmt(a.stop)}</b> ·
                  target <b className="text-gray-200">₹{fmt(a.target)}</b>
                  {' '}| resistance {JSON.stringify(c.resistances)} · support {JSON.stringify(c.supports)}
                </div>
                <Reason text={a.reasons} />
                <div className="mt-2">
                  <button onClick={() => trade(c.symbol, 'BUY')}
                    className="text-xs px-3 py-1 rounded bg-emerald-600 text-white font-semibold">Buy</button>
                </div>
              </div>
            )
          })}
        </div>

        {/* Holdings */}
        <div>
          <h3 className="text-xs font-bold tracking-wider uppercase text-gray-500 mb-3">My holdings — exit advice</h3>
          {positions.length === 0 ? (
            <div className="text-center text-sm text-gray-500 border border-dashed border-gray-800 rounded-lg p-6">No holdings.</div>
          ) : positions.map((p) => {
            const a = advice[p.symbol] || {}
            return (
              <div key={p.symbol} className="bg-gray-900 border border-gray-800 rounded-xl p-4 mb-3">
                <div className="flex items-center gap-2">
                  <span className="font-bold text-base text-white">{p.name}</span>
                  <span className={`px-2 py-0.5 rounded text-[10px] font-bold uppercase ${
                    p.source === 'ACTUAL' ? 'bg-blue-500/15 text-blue-300' : 'bg-amber-500/15 text-amber-300'}`}>
                    {p.source}
                  </span>
                  <span className="flex-1" /><Pill rec={a} />
                </div>
                <div className="mt-2 text-xs text-gray-500">
                  {p.qty} @ ₹{fmt(p.avg_price)} · LTP ₹{fmt(p.ltp)} ·
                  <b className={p.pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}> P&L ₹{fmt(p.pnl)} ({fmt(p.pnl_pct)}%)</b>
                  {a.key_resistance ? <> | resist <b className="text-gray-200">₹{fmt(a.key_resistance)}</b> · stop <b className="text-gray-200">₹{fmt(a.stop)}</b></> : null}
                </div>
                <Reason text={a.reasons} />
                <div className="mt-2">
                  <button onClick={() => trade(p.symbol, 'SELL')}
                    className="text-xs px-3 py-1 rounded bg-red-600 text-white font-semibold">Sell</button>
                </div>
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
