import { useEffect, useMemo, useState } from 'react'
import { ReportPage } from './components/ReportPage'
import { CandlestickChart } from './components/CandlestickChart'
import { PnLGraph } from './components/PnLGraph'
import { DecisionFeed } from './components/DecisionFeed'
import { PositionTable } from './components/PositionTable'
import { TimeframeSelector } from './components/TimeframeSelector'
import { ContextPanel } from './components/ContextPanel'
import { DecisionTimeline } from './components/DecisionTimeline'
import { useMarketData } from './hooks/useMarketData'
import { useDecisionFeed } from './hooks/useDecisionFeed'
import { usePnL } from './hooks/usePnL'
import { useTradingStore } from './store'
import { TradingModeToggle } from './components/TradingModeToggle'
import { fetchDecisions } from './api/decisionLog'
import { fetchTrades } from './api/trades'
import { fetchPositions } from './api/positions'
import { fetchSymbols } from './api/marketData'
import { fetchHistoricalData, fetchAggregatedView, fetchContextSnapshot, fetchDecisionHistory } from './api/historical'
import type { Timeframe, HistoricalCandle, ContextSnapshot, Decision } from './types'

const SYMBOLS = ['NSE:NIFTY50-INDEX', 'NSE:NIFTYBANK-INDEX']
const MARKET_OPEN_MINUTES = 9 * 60 + 15
const MARKET_CLOSE_MINUTES = 15 * 60 + 30

function isWithinMarketWindow(now: Date): boolean {
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Kolkata',
    weekday: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).formatToParts(now)

  const weekday = parts.find((p) => p.type === 'weekday')?.value ?? 'Mon'
  const hour = Number(parts.find((p) => p.type === 'hour')?.value ?? 0)
  const minute = Number(parts.find((p) => p.type === 'minute')?.value ?? 0)

  if (weekday === 'Sat' || weekday === 'Sun') {
    return false
  }

  const currentMinutes = hour * 60 + minute
  return currentMinutes >= MARKET_OPEN_MINUTES && currentMinutes <= MARKET_CLOSE_MINUTES
}

export default function App() {
  const [page, setPage] = useState<'dashboard' | 'report'>('dashboard')

  const {
    selectedSymbol, setSelectedSymbol,
    marketData, decisions, sseConnected,
    trades, setTrades,
    positions, setPositions,
    setDecisions,
    pnl,
    tradingMode,
  } = useTradingStore()

  useMarketData(SYMBOLS)
  useDecisionFeed()
  usePnL()

  // Historical state
  const [timeframe, setTimeframe] = useState<Timeframe>('5m')
  const [historicalCandles, setHistoricalCandles] = useState<HistoricalCandle[]>([])
  const [context, setContext] = useState<ContextSnapshot | null>(null)
  const [contextLoading, setContextLoading] = useState(false)
  const [decisionHistory, setDecisionHistory] = useState<Decision[]>([])
  const [showHistory, setShowHistory] = useState(false)
  const [now, setNow] = useState(() => new Date())

  // Load initial operational data
  useEffect(() => {
    fetchDecisions({ limit: 50 }).then((r) => setDecisions(r.decisions)).catch(() => {})
    fetchTrades(undefined, 100).then((r) => setTrades(r.trades)).catch(() => {})
    fetchPositions().then((r) => setPositions(r.positions)).catch(() => {})
  }, [])

  // Refresh positions every 15s
  useEffect(() => {
    const id = setInterval(() => {
      fetchPositions().then((r) => setPositions(r.positions)).catch(() => {})
    }, 15000)
    return () => clearInterval(id)
  }, [])

  // Load historical candles when symbol or timeframe changes
  useEffect(() => {
    let cancelled = false
    // Clear immediately so the chart doesn't show the previous symbol's data
    setHistoricalCandles([])
    const load = async () => {
      try {
        let data
        if (timeframe === '1m') {
          data = await fetchHistoricalData(selectedSymbol, '1m', 200)
        } else {
          data = await fetchAggregatedView(selectedSymbol, timeframe, 200)
        }
        if (!cancelled) setHistoricalCandles(data)
      } catch {
        // leave chart blank rather than showing wrong symbol's candles
      }
    }
    load()
    return () => { cancelled = true }
  }, [selectedSymbol, timeframe])

  // Load context snapshot when symbol changes
  useEffect(() => {
    let cancelled = false
    setContextLoading(true)
    fetchContextSnapshot(selectedSymbol)
      .then((data) => { if (!cancelled) setContext(data) })
      .catch(() => { /* keep previous context rather than clearing */ })
      .finally(() => { if (!cancelled) setContextLoading(false) })
    return () => { cancelled = true }
  }, [selectedSymbol])

  // Load decision history from DB when panel is opened
  useEffect(() => {
    if (!showHistory) return
    let cancelled = false
    fetchDecisionHistory(selectedSymbol, 50)
      .then((r) => { if (!cancelled) setDecisionHistory(r.decisions ?? []) })
      .catch(() => { /* keep previous history rather than clearing */ })
    return () => { cancelled = true }
  }, [showHistory, selectedSymbol])

  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 30000)
    return () => clearInterval(id)
  }, [])

  const currentData = marketData[selectedSymbol]
  const marketOpen = useMemo(() => isWithinMarketWindow(now), [now])
  const isLive = sseConnected && marketOpen

  const symbolTrades = useMemo(
    () => trades.filter((t) => t.symbol === selectedSymbol),
    [trades, selectedSymbol],
  )

  const todayTrades = useMemo(() => {
    const today = new Date().toDateString()
    return trades
      .filter((t) => new Date(t.entry_time).toDateString() === today)
      .sort((a, b) => new Date(a.entry_time).getTime() - new Date(b.entry_time).getTime())
  }, [trades])

  // Use live 5m candles when available, otherwise fall back to historical
  const liveCandles = currentData?.candles
  const displayCandles = useMemo(() => {
    if (timeframe === '5m' && liveCandles?.length) {
      return liveCandles
    }
    return historicalCandles.map((c) => ({
      timestamp: c.time,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
      volume: c.volume,
    }))
  }, [timeframe, liveCandles, historicalCandles])

  if (page === 'report') {
    return (
      <div className="min-h-screen bg-gray-950 text-gray-100">
        <nav className="flex items-center gap-1 px-6 py-3 border-b border-gray-800 bg-gray-900">
          <span className="text-sm font-bold text-white mr-4">Trading Intelligence</span>
          <NavTab label="Dashboard" active={false} onClick={() => setPage('dashboard')} />
          <NavTab label="Reports"   active={true}  onClick={() => setPage('report')} />
        </nav>
        <ReportPage />
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-gray-950 text-gray-100 p-4">
      {/* Header */}
      <header className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <h1 className="text-xl font-bold text-white">Trading Intelligence</h1>
          {/* Page nav */}
          <div className="flex items-center bg-gray-800 rounded-md p-0.5 gap-0.5">
            <NavTab label="Dashboard" active={true}  onClick={() => setPage('dashboard')} />
            <NavTab label="Reports"   active={false} onClick={() => setPage('report')} />
          </div>
          <TradingModeToggle />
          <span className={`text-xs px-2 py-0.5 rounded border font-mono ${
            isLive
              ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30'
              : 'bg-amber-500/10 text-amber-400 border-amber-500/30'
          }`}>
            {isLive ? 'MARKET OPEN' : 'MARKET CLOSED'}
          </span>
          {tradingMode === 'live' && (
            <span className="text-xs px-2 py-0.5 rounded border font-mono bg-red-500/10 text-red-400 border-red-500/30 animate-pulse">
              REAL ORDERS ACTIVE
            </span>
          )}
        </div>
        <div className="flex gap-2">
          {SYMBOLS.map((s) => (
            <button
              key={s}
              onClick={() => setSelectedSymbol(s)}
              className={`px-3 py-1.5 rounded text-sm font-mono transition-colors ${
                selectedSymbol === s
                  ? 'bg-blue-600 text-white'
                  : 'bg-gray-800 text-gray-400 hover:bg-gray-700'
              }`}
            >
              {s.replace('NSE:', '').replace('-INDEX', '')}
            </button>
          ))}
        </div>
      </header>

      {/* Price Header */}
      {currentData && (
        <div className="flex items-baseline gap-3 mb-3">
          <span className="text-3xl font-bold font-mono text-white">
            ₹{currentData.ltp.toLocaleString('en-IN')}
          </span>
          <span className={`text-lg font-mono ${currentData.change_pct >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
            {currentData.change_pct >= 0 ? '+' : ''}{currentData.change_pct.toFixed(2)}%
          </span>
          <span className="text-sm text-gray-500 font-mono">
            RSI: {currentData.indicators?.rsi?.toFixed(1)}
          </span>
          <span className="text-sm text-gray-500 font-mono">
            CPR: {currentData.indicators?.cpr_signal?.replace('_CPR', '')}
          </span>
          <span className="text-sm text-gray-500 font-mono">
            VWAP: ₹{currentData.indicators?.vwap?.toLocaleString('en-IN')}
          </span>
        </div>
      )}

      {/* Main grid */}
      <div className="grid grid-cols-3 gap-4">
        {/* Chart area — 2/3 width */}
        <div className="col-span-2 space-y-4">
          <div className="bg-gray-900 rounded-lg border border-gray-800 p-3">
            {/* Chart header: timeframe selector */}
            <div className="flex items-center justify-between mb-2">
              <TimeframeSelector selected={timeframe} onChange={setTimeframe} />
              <div className="flex gap-2">
                <button
                  onClick={() => setShowHistory(!showHistory)}
                  className={`text-xs px-2 py-1 rounded border font-mono transition-colors ${
                    showHistory
                      ? 'bg-purple-600/20 text-purple-400 border-purple-600/40'
                      : 'bg-gray-800 text-gray-400 border-gray-700 hover:bg-gray-700'
                  }`}
                >
                  History
                </button>
              </div>
            </div>

            <CandlestickChart
              key={selectedSymbol}
              candles={displayCandles}
              trades={symbolTrades}
              height={380}
              cprLevels={currentData?.indicators?.cpr}
              pivots={currentData?.indicators?.pivots}
            />
          </div>

          {/* Decision History Timeline (collapsible) */}
          {showHistory && (
            <div className="bg-gray-900 rounded-lg border border-gray-800 p-3">
              <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wider mb-2">
                Decision History (DB)
              </h3>
              <DecisionTimeline decisions={decisionHistory} />
            </div>
          )}

          {/* P&L graph */}
          {pnl && <PnLGraph pnl={pnl} />}

          {/* Positions */}
          <PositionTable positions={positions} trades={todayTrades} />
        </div>

        {/* Right column — 1/3 width */}
        <div className="col-span-1 space-y-4">
          {/* Historical Context Panel */}
          <ContextPanel context={context} loading={contextLoading} />

          {/* Live Decision Feed */}
          <DecisionFeed decisions={decisions} sseConnected={sseConnected} />
        </div>
      </div>
    </div>
  )
}

function NavTab({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={`px-3 py-1 text-xs font-medium rounded transition-colors ${
        active
          ? 'bg-gray-600 text-white'
          : 'text-gray-400 hover:text-gray-200'
      }`}
    >
      {label}
    </button>
  )
}
