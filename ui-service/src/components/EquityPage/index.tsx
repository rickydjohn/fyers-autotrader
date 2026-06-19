import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
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
  if (!rec?.action) return <span className="text-xs text-gray-600">—</span>
  return (
    <span className={`px-2 py-0.5 rounded text-xs font-bold border ${pillClass(rec.action)}`}>
      {rec.action}
    </span>
  )
}

// One labelled metric in the expanded detail grid.
function Stat({ label, value, color }: { label: string; value: ReactNode; color?: string }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-gray-500">{label}</div>
      <div className={`font-mono text-sm ${color ?? 'text-gray-200'}`}>{value}</div>
    </div>
  )
}

function Chevron({ open }: { open: boolean }) {
  return <span className={`text-gray-600 text-xs transition-transform ${open ? 'rotate-90' : ''}`}>▶</span>
}

export function EquityPage() {
  const [mode, setMode] = useState('simulation')
  const [positions, setPositions] = useState<EquityPosition[]>([])
  const [candidates, setCandidates] = useState<EquityCandidate[]>([])
  // full holding analysis card keyed by symbol (recommendation + levels + indicators)
  const [holdAnalysis, setHoldAnalysis] = useState<Record<string, any>>({})
  const [updated, setUpdated] = useState<string | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [expCand, setExpCand] = useState<string | null>(null)
  const [expHold, setExpHold] = useState<string | null>(null)
  const pollRef = useRef<number | null>(null)

  const loadPositions = useCallback(() => {
    fetchEquityPositions().then((d) => setPositions(d.positions || [])).catch(() => {})
  }, [])

  const applyReport = useCallback((report: any) => {
    if (!report) { setUpdated(null); return }
    setUpdated(report.generated_at ?? null)
    const m: Record<string, any> = {}
    ;(report.holdings || []).forEach((c: any) => { m[c.symbol] = c })
    setHoldAnalysis(m)
    setCandidates(report.candidates || [])
  }, [])

  const loadCached = useCallback(() =>
    fetchEquityAnalysisCached().then((d) => applyReport(d.report)).catch(() => {}), [applyReport])

  useEffect(() => {
    getEquityMode().then((d) => setMode(d.mode)).catch(() => {})
    loadCached(); loadPositions()
    return () => { if (pollRef.current) window.clearInterval(pollRef.current) }
  }, [loadCached, loadPositions])

  const toggleMode = async () => {
    const next = mode === 'live' ? 'simulation' : 'live'
    if (next === 'live' && !window.confirm('Switch to LIVE? Buy/Sell will place REAL Fyers orders.')) return
    setMode((await setEquityMode(next)).mode)
  }

  const refresh = async () => {
    const prev = updated
    setRefreshing(true)
    await refreshEquityAnalysis(8).catch(() => {})
    let waited = 0
    if (pollRef.current) window.clearInterval(pollRef.current)
    pollRef.current = window.setInterval(async () => {
      waited += 8
      const r = (await fetchEquityAnalysisCached().catch(() => null))?.report
      if (r && r.generated_at !== prev) {
        window.clearInterval(pollRef.current!); applyReport(r); loadPositions(); setRefreshing(false)
      } else if (waited > 360) { window.clearInterval(pollRef.current!); setRefreshing(false) }
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
      (d.fill ? ` @ ₹${d.fill}` : '') + (d.realized_pnl != null ? ` · P&L ₹${d.realized_pnl}` : '') +
      (d.detail ? ` — ${d.detail}` : ''))
    loadPositions()
  }

  return (
    <div className="p-4">
      <div className="flex items-center gap-3 mb-4">
        <h2 className="text-lg font-bold text-white">Equity Advisor</h2>
        <span className={`px-2.5 py-0.5 rounded-full text-xs font-bold border ${
          mode === 'live' ? 'bg-red-500/10 text-red-400 border-red-500/30'
                          : 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30'}`}>{mode.toUpperCase()}</span>
        <button onClick={toggleMode} className="text-xs px-2 py-1 rounded border border-gray-700 text-gray-300 hover:bg-gray-800">switch</button>
        <span className="flex-1" />
        <span className="text-xs text-gray-500">{updated ? `updated ${new Date(updated).toLocaleString('en-IN')}` : 'no analysis yet'}</span>
        <button onClick={refresh} disabled={refreshing}
          className="text-sm px-3 py-1.5 rounded bg-blue-600 text-white font-semibold disabled:opacity-60">
          {refreshing ? 'Analysing…' : '↻ Refresh'}</button>
      </div>

      <div className="grid grid-cols-2 gap-5 max-[980px]:grid-cols-1">
        {/* Potential stocks */}
        <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
          <div className="px-4 py-2.5 border-b border-gray-800 text-xs font-bold tracking-wider uppercase text-gray-400">
            Potential stocks ({candidates.length})
          </div>
          {candidates.length === 0 ? (
            <div className="p-6 text-center text-sm text-gray-600">No recommendations yet — click ↻ Refresh (≈1–2 min).</div>
          ) : (
            <table className="w-full text-sm">
              <thead><tr className="text-[10px] text-gray-500 uppercase">
                <th className="px-4 py-1.5 text-left">Symbol</th><th className="px-3 py-1.5 text-left">Call</th>
                <th className="px-3 py-1.5 text-right">12m mom</th><th className="px-4 py-1.5 text-right">Price</th></tr></thead>
              <tbody className="divide-y divide-gray-800">
                {candidates.map((c) => {
                  const open = expCand === c.symbol
                  return [
                    <tr key={c.symbol} onClick={() => setExpCand(open ? null : c.symbol)}
                        className="hover:bg-gray-800/50 cursor-pointer select-none">
                      <td className="px-4 py-2.5"><div className="flex items-center gap-2">
                        <Chevron open={open} /><span className="font-mono text-xs text-gray-200">{c.name}</span></div></td>
                      <td className="px-3 py-2.5"><Pill rec={c.recommendation} /></td>
                      <td className="px-3 py-2.5 text-right font-mono text-xs text-gray-300">{fmt(c.momentum_12m_pct)}%</td>
                      <td className="px-4 py-2.5 text-right font-mono text-xs text-gray-300">₹{fmt(c.ltp)}</td>
                    </tr>,
                    open && (
                      <tr key={`${c.symbol}-d`} className="bg-gray-950/60"><td colSpan={4} className="px-4 pb-3 pt-1">
                        <CandidateDetail c={c} onBuy={() => trade(c.symbol, 'BUY')} />
                      </td></tr>
                    ),
                  ]
                })}
              </tbody>
            </table>
          )}
        </div>

        {/* Holdings */}
        <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
          <div className="px-4 py-2.5 border-b border-gray-800 text-xs font-bold tracking-wider uppercase text-gray-400">
            My holdings ({positions.length})
          </div>
          {positions.length === 0 ? (
            <div className="p-6 text-center text-sm text-gray-600">No holdings.</div>
          ) : (
            <table className="w-full text-sm">
              <thead><tr className="text-[10px] text-gray-500 uppercase">
                <th className="px-4 py-1.5 text-left">Symbol</th><th className="px-3 py-1.5 text-left">Call</th>
                <th className="px-4 py-1.5 text-right">P&L</th></tr></thead>
              <tbody className="divide-y divide-gray-800">
                {positions.map((p) => {
                  const open = expHold === p.symbol
                  const card = holdAnalysis[p.symbol]
                  const rec: EquityRec | undefined = card?.recommendation
                  return [
                    <tr key={p.symbol} onClick={() => setExpHold(open ? null : p.symbol)}
                        className="hover:bg-gray-800/50 cursor-pointer select-none">
                      <td className="px-4 py-2.5"><div className="flex items-center gap-2">
                        <Chevron open={open} /><span className="font-mono text-xs text-gray-200">{p.name}</span>
                        <span className={`px-1.5 py-0.5 rounded text-[9px] font-bold uppercase ${
                          p.source === 'ACTUAL' ? 'bg-blue-500/15 text-blue-300' : 'bg-amber-500/15 text-amber-300'}`}>{p.source}</span>
                      </div></td>
                      <td className="px-3 py-2.5"><Pill rec={rec} /></td>
                      <td className={`px-4 py-2.5 text-right font-mono text-xs font-semibold ${p.pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                        {p.pnl >= 0 ? '+' : ''}₹{fmt(p.pnl)} <span className="text-gray-500">({fmt(p.pnl_pct)}%)</span></td>
                    </tr>,
                    open && (
                      <tr key={`${p.symbol}-d`} className="bg-gray-950/60"><td colSpan={3} className="px-4 pb-3 pt-1">
                        <HoldingDetail p={p} card={card} onSell={() => trade(p.symbol, 'SELL')} />
                      </td></tr>
                    ),
                  ]
                })}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  )
}

function DetailShell({ rec, children, action, onAction, actionClass }:
  { rec?: EquityRec; children: ReactNode; action: string; onAction: () => void; actionClass: string }) {
  return (
    <div className="rounded-lg border border-gray-700/60 bg-gray-800/40 p-3 space-y-3">
      <div className="flex items-center gap-2">
        <Pill rec={rec} />
        {rec?.conviction && <span className="text-xs text-gray-500">conviction: {rec.conviction}</span>}
        <span className="flex-1" />
        <button onClick={onAction} className={`text-xs px-3 py-1 rounded font-semibold text-white ${actionClass}`}>{action}</button>
      </div>
      {children}
      {rec?.reasons && (
        <div className="px-3 py-2 bg-gray-950 border border-gray-800 rounded text-[13px] text-gray-300">
          <span className="text-[10px] font-bold tracking-wider text-gray-500 mr-1">WHY</span>{rec.reasons}
        </div>
      )}
    </div>
  )
}

function CandidateDetail({ c, onBuy }: { c: EquityCandidate; onBuy: () => void }) {
  const a = c.recommendation || {}
  return (
    <DetailShell rec={a} action="Buy" onAction={onBuy} actionClass="bg-emerald-600">
      <div className="grid grid-cols-4 gap-3 max-[600px]:grid-cols-2">
        <Stat label="Entry" value={`₹${fmt((a.entry_zone && a.entry_zone[0]) ?? c.ltp)}`} />
        <Stat label="Stop" value={`₹${fmt(a.stop)}`} color="text-red-400" />
        <Stat label="Target" value={`₹${fmt(a.target)}`} color="text-emerald-400" />
        <Stat label="Regime" value={c.regime} />
        <Stat label="12m momentum" value={`${fmt(c.momentum_12m_pct)}%`} />
        <Stat label="From 52w high" value={`${fmt(c.pct_from_52w_high)}%`} />
        <Stat label="Resistance" value={(c.resistances || []).map((r) => `₹${fmt(r)}`).join('  ') || '—'} />
        <Stat label="Support" value={(c.supports || []).map((s) => `₹${fmt(s)}`).join('  ') || '—'} />
      </div>
    </DetailShell>
  )
}

function HoldingDetail({ p, card, onSell }: { p: EquityPosition; card: any; onSell: () => void }) {
  const a: EquityRec = card?.recommendation || {}
  return (
    <DetailShell rec={a} action="Sell" onAction={onSell} actionClass="bg-red-600">
      <div className="grid grid-cols-4 gap-3 max-[600px]:grid-cols-2">
        <Stat label="Qty" value={p.qty} />
        <Stat label="Avg cost" value={`₹${fmt(p.avg_price)}`} />
        <Stat label="LTP" value={`₹${fmt(p.ltp)}`} />
        <Stat label="P&L" value={`${p.pnl >= 0 ? '+' : ''}₹${fmt(p.pnl)} (${fmt(p.pnl_pct)}%)`} color={p.pnl >= 0 ? 'text-emerald-400' : 'text-red-400'} />
        <Stat label="Invested" value={`₹${fmt(p.qty * p.avg_price)}`} />
        <Stat label="Current" value={`₹${fmt(p.qty * p.ltp)}`} />
        {a.key_resistance != null && <Stat label="Key resistance" value={`₹${fmt(a.key_resistance)}`} />}
        {a.stop != null && <Stat label="Suggested stop" value={`₹${fmt(a.stop)}`} color="text-red-400" />}
        {card?.regime && <Stat label="Regime" value={card.regime} />}
        {card?.rsi != null && <Stat label="RSI" value={fmt(card.rsi)} />}
        {card?.momentum_12m_pct != null && <Stat label="12m momentum" value={`${fmt(card.momentum_12m_pct)}%`} />}
        {card?.resistances && <Stat label="Resistance" value={(card.resistances || []).map((r: number) => `₹${fmt(r)}`).join('  ') || '—'} />}
        {card?.supports && <Stat label="Support" value={(card.supports || []).map((s: number) => `₹${fmt(s)}`).join('  ') || '—'} />}
      </div>
      {!card && <div className="text-xs text-gray-600">No analysis yet for this holding — click ↻ Refresh.</div>}
    </DetailShell>
  )
}
