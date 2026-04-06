import { useEffect } from 'react'
import { fetchMarketData } from '../api/marketData'
import { useTradingStore } from '../store'

/**
 * Poll market data for ALL given symbols simultaneously.
 * Data is stored in the store keyed by symbol so switching tabs
 * is instant — no cold start waiting for the first poll cycle.
 */
export function useMarketData(symbols: string[], intervalMs = 10000) {
  const setMarketData = useTradingStore((s) => s.setMarketData)

  useEffect(() => {
    if (!symbols.length) return

    const loadAll = () =>
      Promise.all(
        symbols.map(async (symbol) => {
          try {
            const data = await fetchMarketData(symbol)
            setMarketData(symbol, data)
          } catch {
            // silent — market may be closed or symbol not yet processed
          }
        }),
      )

    loadAll()
    const id = setInterval(loadAll, intervalMs)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbols.join(','), intervalMs, setMarketData])
}
