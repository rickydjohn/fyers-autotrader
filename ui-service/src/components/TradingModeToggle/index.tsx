import { useEffect, useState } from 'react'
import { useTradingStore } from '../../store'
import { fetchFunds, fetchTradingMode, updateTradingMode } from '../../api/tradingMode'
import type { TradingMode } from '../../types'

export function TradingModeToggle() {
  const { tradingMode, setTradingMode, fundsData, setFundsData } = useTradingStore()
  const [loading, setLoading] = useState(false)
  const [showConfirm, setShowConfirm] = useState(false)
  const [pendingMode, setPendingMode] = useState<TradingMode | null>(null)

  // Load initial mode from backend
  useEffect(() => {
    fetchTradingMode()
      .then((r) => setTradingMode(r.mode))
      .catch(() => {})
  }, [])

  // Fetch funds whenever live mode is active
  useEffect(() => {
    if (tradingMode === 'live') {
      fetchFunds()
        .then(setFundsData)
        .catch(() => setFundsData(null))
    } else {
      setFundsData(null)
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
      {/* Mode toggle */}
      <div className="flex rounded-md overflow-hidden border border-gray-700 text-xs font-mono">
        <button
          onClick={() => requestSwitch('simulation')}
          disabled={loading}
          className={`px-3 py-1.5 transition-colors ${
            tradingMode === 'simulation'
              ? 'bg-blue-600 text-white'
              : 'bg-gray-800 text-gray-400 hover:bg-gray-700'
          }`}
        >
          SIMULATION
        </button>
        <button
          onClick={() => requestSwitch('live')}
          disabled={loading}
          className={`px-3 py-1.5 transition-colors ${
            tradingMode === 'live'
              ? 'bg-red-600 text-white'
              : 'bg-gray-800 text-gray-400 hover:bg-gray-700'
          }`}
        >
          LIVE
        </button>
      </div>

      {/* Funds display when in live mode */}
      {tradingMode === 'live' && availableBalance !== null && (
        <span className="text-xs font-mono text-emerald-400 bg-emerald-500/10 border border-emerald-500/30 px-2 py-1 rounded">
          Avail: ₹{availableBalance.toLocaleString('en-IN', { maximumFractionDigits: 0 })}
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
