import { useEffect, useRef, useState } from 'react'
import { useTradingStore } from '../../store'
import { fetchFunds, fetchSimulationBudget, fetchTradingMode, updateTradingMode } from '../../api/tradingMode'
import type { TradingMode } from '../../types'

export function TradingModeToggle() {
  const {
    tradingMode, setTradingMode,
    fundsData, setFundsData,
    simulationBudget, setSimulationBudget,
  } = useTradingStore()
  const [loading, setLoading] = useState(false)
  const [showConfirm, setShowConfirm] = useState(false)
  const [pendingMode, setPendingMode] = useState<TradingMode | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // Sync with backend on mount — but never override a persisted 'live' mode with simulation
  useEffect(() => {
    fetchTradingMode()
      .then((r) => {
        // Only apply backend mode if it's 'live', or if we're currently in simulation
        if (r.mode === 'live' || tradingMode === 'simulation') {
          setTradingMode(r.mode)
        }
      })
      .catch(() => {})
  }, [])

  // Fetch mode-specific balances whenever trading mode changes
  useEffect(() => {
    if (tradingMode === 'live') {
      fetchFunds()
        .then(setFundsData)
        .catch(() => setFundsData(null))
      setSimulationBudget(null)

      // Poll live funds every 30 seconds
      pollRef.current = setInterval(() => {
        fetchFunds()
          .then(setFundsData)
          .catch(() => {})
      }, 30_000)
    } else {
      if (pollRef.current) {
        clearInterval(pollRef.current)
        pollRef.current = null
      }
      setFundsData(null)
      fetchSimulationBudget()
        .then(setSimulationBudget)
        .catch(() => setSimulationBudget(null))
    }

    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current)
        pollRef.current = null
      }
    }
  }, [tradingMode])

  function requestSwitch(mode: TradingMode) {
    if (mode === tradingMode || loading) return
    if (mode === 'live') {
      setPendingMode(mode)
      setShowConfirm(true)
    } else {
      applyMode(mode)
    }
  }

  async function applyMode(mode: TradingMode) {
    setLoading(true)
    setShowConfirm(false)
    try {
      const r = await updateTradingMode(mode)
      setTradingMode(r.mode)
    } catch {
      // mode stays unchanged
    } finally {
      setLoading(false)
      setPendingMode(null)
    }
  }

  const availableBalance =
    fundsData?.available_balance ?? fundsData?.net_available ?? null

  return (
    <div className="flex items-center gap-3">
      {/* Segmented mode control */}
      <div className={`flex items-center rounded-md overflow-hidden border text-xs font-mono font-semibold tracking-wide transition-colors ${
        tradingMode === 'live' ? 'border-red-500/50' : 'border-blue-500/30'
      } ${loading ? 'opacity-60 pointer-events-none' : ''}`}>
        <button
          onClick={() => requestSwitch('simulation')}
          className={`px-3 py-1.5 transition-colors ${
            tradingMode === 'simulation'
              ? 'bg-blue-600 text-white'
              : 'bg-gray-800/80 text-gray-400 hover:bg-gray-700 hover:text-gray-200'
          }`}
          aria-pressed={tradingMode === 'simulation'}
        >
          SIM
        </button>
        <div className={`w-px self-stretch ${tradingMode === 'live' ? 'bg-red-500/40' : 'bg-blue-500/20'}`} />
        <button
          onClick={() => requestSwitch('live')}
          className={`px-3 py-1.5 transition-colors ${
            tradingMode === 'live'
              ? 'bg-red-600 text-white'
              : 'bg-gray-800/80 text-gray-400 hover:bg-gray-700 hover:text-gray-200'
          }`}
          aria-pressed={tradingMode === 'live'}
        >
          {loading ? '...' : 'LIVE'}
        </button>
      </div>

      {/* Mode-specific balance display */}
      {tradingMode === 'live' && availableBalance !== null && (
        <span className="text-xs font-mono text-emerald-400 bg-emerald-500/10 border border-emerald-500/30 px-2 py-1 rounded">
          Funds: ₹{availableBalance.toLocaleString('en-IN', { maximumFractionDigits: 0 })}
        </span>
      )}
      {tradingMode === 'simulation' && simulationBudget && (
        <span className="text-xs font-mono text-blue-300 bg-blue-500/10 border border-blue-500/30 px-2 py-1 rounded">
          Sim Budget: ₹{simulationBudget.current.toLocaleString('en-IN', { maximumFractionDigits: 0 })}
        </span>
      )}

      {/* Confirmation modal for switching to live */}
      {showConfirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70">
          <div className="bg-gray-900 border border-red-500/50 rounded-lg p-6 max-w-sm w-full mx-4 shadow-xl">
            <h3 className="text-red-400 font-bold text-base mb-2">Switch to Live Trading?</h3>
            <p className="text-gray-300 text-sm mb-1">
              This will execute <span className="text-white font-semibold">real orders</span> using
              actual funds in your Fyers account.
            </p>
            <p className="text-gray-400 text-xs mb-5">
              Stop-loss and target monitoring will continue to work. Ensure Fyers is authenticated
              before proceeding.
            </p>
            <div className="flex gap-3 justify-end">
              <button
                onClick={() => { setShowConfirm(false); setPendingMode(null) }}
                className="px-4 py-2 rounded bg-gray-700 text-gray-300 text-sm hover:bg-gray-600 transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={() => pendingMode && applyMode(pendingMode)}
                className="px-4 py-2 rounded bg-red-600 text-white text-sm font-semibold hover:bg-red-500 transition-colors"
              >
                Enable Live Trading
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
