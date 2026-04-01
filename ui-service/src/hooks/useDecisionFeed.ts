import { useEffect, useRef } from 'react'
import { SSE_URL } from '../api/client'
import { useTradingStore } from '../store'
import type { Decision } from '../types'

export function useDecisionFeed() {
  const addDecision = useTradingStore((s) => s.addDecision)
  const setSseConnected = useTradingStore((s) => s.setSseConnected)
  const esRef = useRef<EventSource | null>(null)
  const retryRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const retryDelay = useRef(1000)

  useEffect(() => {
    const connect = () => {
      const es = new EventSource(SSE_URL)
      esRef.current = es

      es.onopen = () => {
        setSseConnected(true)
        retryDelay.current = 1000
      }

      es.addEventListener('decision', (e) => {
        try {
          const data = JSON.parse(e.data) as Decision
          addDecision(data)
        } catch {
          // skip malformed
        }
      })

      es.onerror = () => {
        setSseConnected(false)
        es.close()
        // Exponential backoff, max 30s
        retryDelay.current = Math.min(retryDelay.current * 2, 30000)
        retryRef.current = setTimeout(connect, retryDelay.current)
      }
    }

    connect()
    return () => {
      esRef.current?.close()
      if (retryRef.current) clearTimeout(retryRef.current)
    }
  }, [addDecision, setSseConnected])
}
