import { useEffect, useState } from 'react'
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

  // Load initial mode from backend
  useEffect(() => {
    fetchTradingMode()
      .then((r) => setTradingMode(r.mode))
      .catch(() => {})
  }, [])

  // Fetch mode-specific balances whenever trading mode changes
  useEffect(() => {
    if (tradingMode === 'live') {
      fetchFunds()
        .then(setFundsData)
        .catch(() => setFundsData(null))
      setSimulationBudget(null)
    } else {
      setFundsData(null)
      fetchSimulationBudget()
        .then(setSimulationBudget)
        .catch(() => setSimulationBudget(null))
    }
  }, [tradingMode])

  function requestSwitch(mode: TradingMode) {
    if (mode === tradingMode) return
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
      <div className="flex items-center gap-2 text-xs font-mono">
        <span className={`${tradingMode === 'simulation' ? 'text-blue-300' : 'text-gray-500'}`}>SIM</span>
        <button
          onClick={() => requestSwitch(tradingMode === 'simulation' ? 'live' : 'simulation')}
          disabled={loading}
          className={`relative h-6 w-12 rounded-full border transition-colors ${
            tradingMode === 'live'
              ? 'bg-red-600/80 border-red-500'
              : 'bg-blue-600/80 border-blue-500'
          } ${loading ? 'opacity-70 cursor-not-allowed' : ''}`}
          aria-label="Toggle trading mode"
          aria-pressed={tradingMode === 'live'}
        >
          <span
            className={`absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform ${
              tradingMode === 'live' ? 'translate-x-6' : 'translate-x-0.5'
            }`}
          />
        </button>
        <span className={`${tradingMode === 'live' ? 'text-red-300' : 'text-gray-500'}`}>LIVE</span>
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
