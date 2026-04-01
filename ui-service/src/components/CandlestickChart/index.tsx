import { useEffect, useRef } from 'react'
import {
  createChart,
  ColorType,
  type IChartApi,
  type ISeriesApi,
  type CandlestickSeriesOptions,
} from 'lightweight-charts'
import type { OHLCBar, Trade } from '../../types'

interface Props {
  candles: OHLCBar[]
  trades?: Trade[]
  height?: number
  cprLevels?: { bc: number; tc: number; pivot: number }
  pivots?: { r1: number; r2: number; s1: number; s2: number }
}

export function CandlestickChart({ candles, trades = [], height = 400, cprLevels, pivots }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const candleSeriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)

  useEffect(() => {
    if (!containerRef.current) return

    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: '#0d1117' },
        textColor: '#8b949e',
      },
      grid: {
        vertLines: { color: '#21262d' },
        horzLines: { color: '#21262d' },
      },
      crosshair: { mode: 1 },
      rightPriceScale: { borderColor: '#30363d' },
      timeScale: { borderColor: '#30363d', timeVisible: true },
      width: containerRef.current.clientWidth,
      height,
    })

    const candleSeries = chart.addCandlestickSeries({
      upColor: '#3fb950',
      downColor: '#f85149',
      borderVisible: false,
      wickUpColor: '#3fb950',
      wickDownColor: '#f85149',
    })

    chartRef.current = chart
    candleSeriesRef.current = candleSeries

    const resizeObserver = new ResizeObserver(() => {
      if (containerRef.current) {
        chart.applyOptions({ width: containerRef.current.clientWidth })
      }
    })
    resizeObserver.observe(containerRef.current)

    return () => {
      resizeObserver.disconnect()
      chart.remove()
    }
  }, [height])

  useEffect(() => {
    if (!candleSeriesRef.current || !candles.length) return

    const chartData = candles.map((c) => ({
      time: Math.floor(new Date(c.timestamp).getTime() / 1000) as any,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    }))

    candleSeriesRef.current.setData(chartData)

    // Add CPR lines as price lines
    if (cprLevels && candleSeriesRef.current) {
      candleSeriesRef.current.createPriceLine({ price: cprLevels.tc, color: '#58a6ff', lineWidth: 1, lineStyle: 2, title: 'TC' })
      candleSeriesRef.current.createPriceLine({ price: cprLevels.pivot, color: '#f0883e', lineWidth: 1, lineStyle: 2, title: 'Pivot' })
      candleSeriesRef.current.createPriceLine({ price: cprLevels.bc, color: '#58a6ff', lineWidth: 1, lineStyle: 2, title: 'BC' })
    }

    if (pivots && candleSeriesRef.current) {
      candleSeriesRef.current.createPriceLine({ price: pivots.r1, color: '#f85149', lineWidth: 1, lineStyle: 3, title: 'R1' })
      candleSeriesRef.current.createPriceLine({ price: pivots.r2, color: '#f85149', lineWidth: 1, lineStyle: 3, title: 'R2' })
      candleSeriesRef.current.createPriceLine({ price: pivots.s1, color: '#3fb950', lineWidth: 1, lineStyle: 3, title: 'S1' })
      candleSeriesRef.current.createPriceLine({ price: pivots.s2, color: '#3fb950', lineWidth: 1, lineStyle: 3, title: 'S2' })
    }

    // Trade markers
    if (trades.length) {
      const markers = trades
        .filter((t) => t.entry_time)
        .map((t) => ({
          time: Math.floor(new Date(t.entry_time).getTime() / 1000) as any,
          position: t.side === 'BUY' ? 'belowBar' as const : 'aboveBar' as const,
          color: t.side === 'BUY' ? '#3fb950' : '#f85149',
          shape: t.side === 'BUY' ? 'arrowUp' as const : 'arrowDown' as const,
          text: t.side,
          size: 1,
        }))
      candleSeriesRef.current.setMarkers(markers)
    }

    chartRef.current?.timeScale().fitContent()
  }, [candles, trades, cprLevels, pivots])

  return <div ref={containerRef} className="w-full rounded-lg overflow-hidden" style={{ height }} />
}
