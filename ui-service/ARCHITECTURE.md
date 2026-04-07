# UI Service — Architecture

The UI is a React + TypeScript SPA. It polls REST endpoints for positions, trades, and P&L, connects to the SSE stream for live decisions, and presents everything in a single-page dashboard with an optional reports view.

## Component Tree

```mermaid
graph TD
    APP["App.tsx\n(root)"]

    subgraph "Dashboard Page"
        HDR["Header\nSymbol selector · Mode toggle · Market status"]
        PRICE["Price Header\nLTP · Change · RSI · CPR · VWAP"]
        CHART_WRAP["Chart Area (2/3 width)"]
        RIGHT["Right Column (1/3 width)"]
    end

    subgraph "Chart Area"
        TF["TimeframeSelector\n1m · 5m · 15m · 1h · 1d"]
        HIST_BTN["History toggle button"]
        CANDLE["CandlestickChart\n(lightweight-charts)\nCandles · CPR · Pivots · Trade markers"]
        TIMELINE["DecisionTimeline\n(collapsible)\nHistorical decisions from DB"]
        PNLG["PnLGraph\n(recharts AreaChart)\nCumulative P&L over time"]
        POSTBL["PositionTable\nOpen Positions tab · Today Trades tab"]
    end

    subgraph "Right Column"
        CTX["ContextPanel\n5-day historical context\nper timeframe"]
        FEED["DecisionFeed\nLive SSE decisions\nBUY/SELL/HOLD cards"]
    end

    subgraph "Reports Page"
        RPT["ReportPage\nMonth picker · Stat cards\nPie charts · Trade table (25/page)"]
    end

    APP --> HDR
    APP --> PRICE
    APP --> CHART_WRAP
    APP --> RIGHT
    APP -->|"page=report"| RPT

    CHART_WRAP --> TF
    CHART_WRAP --> HIST_BTN
    CHART_WRAP --> CANDLE
    CHART_WRAP --> TIMELINE
    CHART_WRAP --> PNLG
    CHART_WRAP --> POSTBL

    RIGHT --> CTX
    RIGHT --> FEED
```

## State Management

```mermaid
graph TD
    subgraph "Zustand Store (store/index.ts)"
        SS["selectedSymbol"]
        MD["marketData\nRecord of symbol → MarketSnapshot"]
        DEC["decisions\nDecision[]"]
        TR["trades\nTrade[]"]
        POS["positions\nPosition[]"]
        PNL_S["pnl\nPnLResponse | null"]
        MODE_S["tradingMode\nsimulation | live"]
        SSE_S["sseConnected\nboolean"]
    end

    subgraph "React Hooks"
        HMD["useMarketData(symbols)\npolls /market-data every 5s\nupdates marketData"]
        HDF["useDecisionFeed()\nSSE /decision-log/stream\naddDecision() on event\nexponential backoff reconnect"]
        HPL["usePnL()\npolls /pnl every 30s\nupdates pnl"]
    end

    subgraph "App.tsx Effects"
        EFF1["useEffect → fetchDecisions\nfetchTrades · fetchPositions\non mount"]
        EFF2["useEffect → fetchPositions\nsetInterval every 15s"]
        EFF3["useEffect → fetchHistoricalData\nor fetchAggregatedView\non symbol/timeframe change"]
        EFF4["useEffect → fetchContextSnapshot\non symbol change"]
        EFF5["useEffect → fetchDecisionHistory\nwhen showHistory toggled"]
    end

    HMD --> MD
    HDF --> DEC & SSE_S
    HPL --> PNL_S
    EFF1 --> DEC & TR & POS
    EFF2 --> POS
    EFF3 -->|"historicalCandles"| LOCAL1["local state"]
    EFF4 -->|"context"| LOCAL2["local state"]
    EFF5 -->|"decisionHistory"| LOCAL3["local state"]
```

## Data Fetching Map

```mermaid
flowchart LR
    subgraph "API Functions (api/)"
        A1["fetchMarketData()\nGET /market-data"]
        A2["fetchDecisions()\nGET /decision-log"]
        A3["fetchTrades()\nGET /trades"]
        A4["fetchPositions()\nGET /positions"]
        A5["fetchPnL()\nGET /pnl"]
        A6["fetchHistoricalData()\nGET /historical-data"]
        A7["fetchAggregatedView()\nGET /aggregated-view"]
        A8["fetchContextSnapshot()\nGET /context-snapshot"]
        A9["fetchDecisionHistory()\nGET /decision-history\n(normalises time→timestamp)"]
        A10["fetchMonthReport()\nGET /report/trades"]
        SSE_F["SSE listener\n/decision-log/stream"]
    end

    A1 -->|"every 5s"| MD["marketData store"]
    A2 -->|"on mount"| DEC_S["decisions store"]
    A3 -->|"on mount"| TRD_S["trades store"]
    A4 -->|"on mount + every 15s"| POS_S["positions store"]
    A5 -->|"every 30s"| PNL_S["pnl store"]
    A6 & A7 --> HC["historicalCandles\nlocal state"]
    A8 --> CTX_L["context\nlocal state"]
    A9 --> DH["decisionHistory\nlocal state"]
    A10 --> RPT_L["report\nlocal state (ReportPage)"]
    SSE_F -->|"addDecision()"| DEC_S
```

## CandlestickChart Render Logic

```mermaid
flowchart TD
    MOUNT(["Component mounts"]) --> CREATE["createChart()\n(lightweight-charts)"]
    CREATE --> ADD_SERIES["addCandlestickSeries()"]
    ADD_SERIES --> RESIZE["ResizeObserver\nauto-width on container resize"]

    PROPS_CHANGE(["candles / cprLevels /\npivots / trades change"]) --> CHECK{Same dataset?\nfirstTime + length unchanged?}
    CHECK -->|"yes (last bar update)"| UPDATE["series.update(lastBar)\n(no canvas flash)"]
    CHECK -->|"no (new symbol / TF)"| SET_DATA["series.setData(allBars)\n+ fitContent()"]

    SET_DATA & UPDATE --> PRICE_LINES["Remove old price lines\nRecreate CPR (TC · Pivot · BC)\nRecreate Pivots (R1·R2·S1·S2)"]
    PRICE_LINES --> MARKERS["series.setMarkers()\nBUY: arrowUp below bar\nSELL: arrowDown above bar"]
```

## Candle Display Decision

```mermaid
flowchart LR
    TF_SEL["Selected Timeframe"] --> IS5M{5m selected?}
    IS5M -->|yes| LIVE_C{Live 5m candles\nin marketData?}
    LIVE_C -->|yes| SHOW_LIVE["Display live candles\n(from SSE/polling)"]
    LIVE_C -->|no| SHOW_HIST["Display historical candles\n(from /aggregated-view)"]
    IS5M -->|"1m, 15m, 1h, 1d"| FETCH_HIST["fetchHistoricalData or\nfetchAggregatedView\non symbol/TF change"]
    FETCH_HIST --> SHOW_HIST
```

## ReportPage Structure

```mermaid
graph TD
    RP["ReportPage"] --> MONTH["Month picker input\n(type=month)"]
    MONTH -->|"onChange"| FETCH_RPT["fetchMonthReport(month)"]
    FETCH_RPT --> STATS["7 StatCards\nTotal · Winners · Losers\nWin Rate · Net P&L\nGross Profit · Gross Loss"]
    FETCH_RPT --> CHARTS["2 Pie Charts\nP&L by Exit Reason\nTrade Count by Exit Reason"]
    FETCH_RPT --> TABLE["TradeTable\n25 trades per page\nDate · Symbol · Entry · Exit\nP&L · % · Exit Reason · Reasoning"]
    TABLE --> PAGES["Prev / Next pagination\n(page state, totalPages)"]
```
