import { useEffect } from 'react'
import { fetchMarketData } from '../api/marketData'
import { useTradingStore } from '../store'

export function useMarketData(symbol: string, intervalMs = 10000) {
  const setMarketData = useTradingStore((s) => s.setMarketData)

  useEffect(() => {
    if (!symbol) return

    const load = async () => {
      try {
        const data = await fetchMarketData(symbol)
        setMarketData(symbol, data)
      } catch {
        // silent - no data yet
      }
    }

    load()
    const id = setInterval(load, intervalMs)
    return () => clearInterval(id)
  }, [symbol, intervalMs, setMarketData])
}
