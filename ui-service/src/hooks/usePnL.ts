import { useEffect } from 'react'
import { fetchPnL } from '../api/pnl'
import { useTradingStore } from '../store'

export function usePnL(intervalMs = 30000) {
  const setPnL = useTradingStore((s) => s.setPnL)
  const tradingMode = useTradingStore((s) => s.tradingMode)

  useEffect(() => {
    const load = async () => {
      try {
        const data = await fetchPnL()
        setPnL(data)
      } catch {
        // silent
      }
    }
    load()
    const id = setInterval(load, intervalMs)
    return () => clearInterval(id)
  }, [intervalMs, setPnL, tradingMode])
}
