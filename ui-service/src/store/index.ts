import { create } from 'zustand'
import type { Decision, FundsData, MarketData, PnLData, Position, SimulationBudgetData, Trade, TradingMode } from '../types'

interface TradingStore {
  selectedSymbol: string
  setSelectedSymbol: (s: string) => void

  marketData: Record<string, MarketData>
  setMarketData: (symbol: string, data: MarketData) => void

  decisions: Decision[]
  addDecision: (d: Decision) => void
  setDecisions: (ds: Decision[]) => void

  trades: Trade[]
  setTrades: (ts: Trade[]) => void

  positions: Position[]
  setPositions: (ps: Position[]) => void

  pnl: PnLData | null
  setPnL: (p: PnLData) => void

  sseConnected: boolean
  setSseConnected: (v: boolean) => void

  tradingMode: TradingMode
  setTradingMode: (m: TradingMode) => void

  fundsData: FundsData | null
  setFundsData: (f: FundsData | null) => void

  simulationBudget: SimulationBudgetData | null
  setSimulationBudget: (b: SimulationBudgetData | null) => void
}

export const useTradingStore = create<TradingStore>((set) => ({
  selectedSymbol: 'NSE:NIFTY50-INDEX',
  setSelectedSymbol: (s) => set({ selectedSymbol: s }),

  marketData: {},
  setMarketData: (symbol, data) =>
    set((state) => ({ marketData: { ...state.marketData, [symbol]: data } })),

  decisions: [],
  addDecision: (d) =>
    set((state) => ({
      decisions: [d, ...state.decisions].slice(0, 100),
    })),
  setDecisions: (ds) => set({ decisions: ds }),

  trades: [],
  setTrades: (ts) => set({ trades: ts }),

  positions: [],
  setPositions: (ps) => set({ positions: ps }),

  pnl: null,
  setPnL: (p) => set({ pnl: p }),

  sseConnected: false,
  setSseConnected: (v) => set({ sseConnected: v }),

  tradingMode: 'simulation',
  setTradingMode: (m) => set({ tradingMode: m }),

  fundsData: null,
  setFundsData: (f) => set({ fundsData: f }),

  simulationBudget: null,
  setSimulationBudget: (b) => set({ simulationBudget: b }),
}))
