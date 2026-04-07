-- ============================================================
-- Trading Intelligence System — TimescaleDB Schema
-- ============================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ============================================================
-- 1. Market Candles (1-minute base ticks from Fyers)
-- ============================================================
CREATE TABLE IF NOT EXISTS market_candles (
    time        TIMESTAMPTZ     NOT NULL,
    symbol      TEXT            NOT NULL,
    open        NUMERIC(12, 2)  NOT NULL,
    high        NUMERIC(12, 2)  NOT NULL,
    low         NUMERIC(12, 2)  NOT NULL,
    close       NUMERIC(12, 2)  NOT NULL,
    volume      BIGINT          NOT NULL DEFAULT 0,
    vwap        NUMERIC(12, 2),
    rsi         NUMERIC(6, 3),
    ema_9       NUMERIC(12, 2),
    ema_21      NUMERIC(12, 2)
);

SELECT create_hypertable(
    'market_candles', 'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE UNIQUE INDEX IF NOT EXISTS market_candles_time_symbol_idx
    ON market_candles (time, symbol);

CREATE INDEX IF NOT EXISTS market_candles_symbol_time_idx
    ON market_candles (symbol, time DESC);

-- ============================================================
-- 2. Daily Indicators (CPR, Pivots — computed once per day)
-- ============================================================
CREATE TABLE IF NOT EXISTS daily_indicators (
    date            DATE            NOT NULL,
    symbol          TEXT            NOT NULL,
    prev_high       NUMERIC(12, 2)  NOT NULL,
    prev_low        NUMERIC(12, 2)  NOT NULL,
    prev_close      NUMERIC(12, 2)  NOT NULL,
    pivot           NUMERIC(12, 2)  NOT NULL,
    bc              NUMERIC(12, 2)  NOT NULL,
    tc              NUMERIC(12, 2)  NOT NULL,
    r1              NUMERIC(12, 2)  NOT NULL,
    r2              NUMERIC(12, 2)  NOT NULL,
    r3              NUMERIC(12, 2)  NOT NULL,
    s1              NUMERIC(12, 2)  NOT NULL,
    s2              NUMERIC(12, 2)  NOT NULL,
    s3              NUMERIC(12, 2)  NOT NULL,
    cpr_width_pct   NUMERIC(6, 4)   NOT NULL,
    PRIMARY KEY (date, symbol)
);

-- ============================================================
-- 3. AI Decisions (LLM outputs)
-- ============================================================
CREATE TABLE IF NOT EXISTS ai_decisions (
    decision_id         TEXT            PRIMARY KEY,
    time                TIMESTAMPTZ     NOT NULL,
    symbol              TEXT            NOT NULL,
    decision            TEXT            NOT NULL CHECK (decision IN ('BUY', 'SELL', 'HOLD')),
    confidence          NUMERIC(4, 3)   NOT NULL,
    reasoning           TEXT            NOT NULL,
    stop_loss           NUMERIC(12, 2)  NOT NULL DEFAULT 0,
    target              NUMERIC(12, 2)  NOT NULL DEFAULT 0,
    risk_reward         NUMERIC(6, 2)   NOT NULL DEFAULT 0,
    indicators_snapshot JSONB,
    acted_upon          BOOLEAN         NOT NULL DEFAULT FALSE,
    trade_id            TEXT,
    historical_context  JSONB
);

-- NOTE:
-- Keep ai_decisions as a regular table (not hypertable) because decision_id
-- is the primary key used by upserts; Timescale hypertables require unique
-- constraints/indexes to include the partitioning column (time).

CREATE INDEX IF NOT EXISTS ai_decisions_symbol_time_idx
    ON ai_decisions (symbol, time DESC);

-- ============================================================
-- 4. Trades (simulation execution records)
-- ============================================================
CREATE TABLE IF NOT EXISTS trades (
    trade_id    TEXT            PRIMARY KEY,
    symbol      TEXT            NOT NULL,
    side        TEXT            NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity    INTEGER         NOT NULL,
    entry_price NUMERIC(12, 2)  NOT NULL,
    entry_time  TIMESTAMPTZ     NOT NULL,
    exit_price  NUMERIC(12, 2),
    exit_time   TIMESTAMPTZ,
    pnl         NUMERIC(12, 2),
    pnl_pct     NUMERIC(8, 4),
    commission  NUMERIC(10, 2)  NOT NULL DEFAULT 0,
    slippage    NUMERIC(10, 2)  NOT NULL DEFAULT 0,
    status      TEXT            NOT NULL DEFAULT 'OPEN',
    decision_id TEXT,
    reasoning   TEXT,
    trading_mode TEXT           NOT NULL DEFAULT 'simulation' CHECK (trading_mode IN ('simulation', 'live')),
    exit_reason  TEXT
);

DO $$
BEGIN
    IF to_regclass('public.trades') IS NOT NULL THEN
        CREATE INDEX IF NOT EXISTS trades_symbol_entry_time_idx
            ON trades (symbol, entry_time DESC);

        CREATE INDEX IF NOT EXISTS trades_entry_time_idx
            ON trades (entry_time DESC);

        CREATE INDEX IF NOT EXISTS trades_mode_entry_time_idx
            ON trades (trading_mode, entry_time DESC);
    END IF;
END $$;

-- ============================================================
-- 5. News Items
-- ============================================================
CREATE TABLE IF NOT EXISTS news_items (
    id              BIGSERIAL       PRIMARY KEY,
    time            TIMESTAMPTZ     NOT NULL,
    title           TEXT            NOT NULL,
    summary         TEXT,
    source          TEXT            NOT NULL,
    sentiment_score NUMERIC(4, 3)   NOT NULL DEFAULT 0.0
);

SELECT create_hypertable(
    'news_items', 'time',
    chunk_time_interval => INTERVAL '7 days',
    migrate_data        => TRUE,
    if_not_exists       => TRUE
);

-- ============================================================
-- 6. Multi-Timeframe Continuous Aggregates
-- ============================================================

-- 5-minute OHLCV
CREATE MATERIALIZED VIEW IF NOT EXISTS candles_5m
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('5 minutes', time)  AS bucket,
    symbol,
    first(open,  time)              AS open,
    max(high)                       AS high,
    min(low)                        AS low,
    last(close,  time)              AS close,
    sum(volume)                     AS volume,
    avg(vwap)                       AS vwap_avg
FROM market_candles
GROUP BY bucket, symbol
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'candles_5m',
    start_offset => INTERVAL '1 day',
    end_offset   => INTERVAL '5 minutes',
    schedule_interval => INTERVAL '5 minutes',
    if_not_exists => TRUE
);

-- 15-minute OHLCV
CREATE MATERIALIZED VIEW IF NOT EXISTS candles_15m
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('15 minutes', time) AS bucket,
    symbol,
    first(open,  time)              AS open,
    max(high)                       AS high,
    min(low)                        AS low,
    last(close,  time)              AS close,
    sum(volume)                     AS volume,
    avg(vwap)                       AS vwap_avg
FROM market_candles
GROUP BY bucket, symbol
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'candles_15m',
    start_offset => INTERVAL '3 days',
    end_offset   => INTERVAL '15 minutes',
    schedule_interval => INTERVAL '15 minutes',
    if_not_exists => TRUE
);

-- 1-hour OHLCV
CREATE MATERIALIZED VIEW IF NOT EXISTS candles_1h
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', time)     AS bucket,
    symbol,
    first(open,  time)              AS open,
    max(high)                       AS high,
    min(low)                        AS low,
    last(close,  time)              AS close,
    sum(volume)                     AS volume,
    avg(vwap)                       AS vwap_avg
FROM market_candles
GROUP BY bucket, symbol
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'candles_1h',
    start_offset => INTERVAL '7 days',
    end_offset   => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists => TRUE
);

-- Daily OHLCV
CREATE MATERIALIZED VIEW IF NOT EXISTS candles_daily
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 day', time)      AS bucket,
    symbol,
    first(open,  time)              AS open,
    max(high)                       AS high,
    min(low)                        AS low,
    last(close,  time)              AS close,
    sum(volume)                     AS volume,
    avg(vwap)                       AS vwap_avg
FROM market_candles
GROUP BY bucket, symbol
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'candles_daily',
    start_offset => INTERVAL '30 days',
    end_offset   => INTERVAL '1 day',
    schedule_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

-- ============================================================
-- 7. Compression (keep hot data 7 days, compress older)
-- ============================================================
ALTER TABLE market_candles SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol'
);

SELECT add_compression_policy(
    'market_candles',
    INTERVAL '7 days',
    if_not_exists => TRUE
);

-- ai_decisions compression policy intentionally omitted because it is not a hypertable.

-- ============================================================
-- 8. Retention (optional — keep 90 days of 1-min candles)
-- ============================================================
SELECT add_retention_policy(
    'market_candles',
    INTERVAL '90 days',
    if_not_exists => TRUE
);

-- ============================================================
-- 9. Multi-year Daily OHLCV  (permanent — no retention)
-- ============================================================
-- Raw daily bars spanning several years, used to compute long-term S/R levels.
-- Deliberately NOT a hypertable so it is never subject to retention policies.
CREATE TABLE IF NOT EXISTS daily_ohlcv (
    date    DATE            NOT NULL,
    symbol  TEXT            NOT NULL,
    open    NUMERIC(12, 2)  NOT NULL,
    high    NUMERIC(12, 2)  NOT NULL,
    low     NUMERIC(12, 2)  NOT NULL,
    close   NUMERIC(12, 2)  NOT NULL,
    volume  BIGINT          NOT NULL DEFAULT 0,
    PRIMARY KEY (date, symbol)
);

CREATE INDEX IF NOT EXISTS daily_ohlcv_symbol_date_idx
    ON daily_ohlcv (symbol, date DESC);

-- ============================================================
-- 10. Historical Support/Resistance Levels
-- ============================================================
-- Computed swing-high/low clusters from the multi-year daily chart.
-- Recomputed on every cluster bootstrap and weekly thereafter.
CREATE TABLE IF NOT EXISTS historical_sr_levels (
    id          BIGSERIAL       PRIMARY KEY,
    symbol      TEXT            NOT NULL,
    level       NUMERIC(12, 2)  NOT NULL,
    level_type  TEXT            NOT NULL
                    CHECK (level_type IN ('SUPPORT', 'RESISTANCE', 'BOTH')),
    strength    INTEGER         NOT NULL DEFAULT 1,
    first_seen  DATE,
    last_seen   DATE,
    computed_at TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS historical_sr_symbol_level_idx
    ON historical_sr_levels (symbol, level);

CREATE INDEX IF NOT EXISTS historical_sr_symbol_strength_idx
    ON historical_sr_levels (symbol, strength DESC);

-- ============================================================
-- 11. Retention Policies
-- ============================================================
-- 1-min base candles: reduce to 30 days — 5m/15m/1h/daily aggregates
-- already materialise older data so raw 1m rows beyond 30 days are redundant.
SELECT remove_retention_policy('market_candles', if_not_exists => TRUE);
SELECT add_retention_policy(
    'market_candles',
    INTERVAL '30 days',
    if_not_exists => TRUE
);

-- Continuous aggregate retention (each view is a Timescale hypertable internally)
SELECT add_retention_policy(
    'candles_5m',
    INTERVAL '90 days',
    if_not_exists => TRUE
);

SELECT add_retention_policy(
    'candles_15m',
    INTERVAL '180 days',
    if_not_exists => TRUE
);

SELECT add_retention_policy(
    'candles_1h',
    INTERVAL '365 days',
    if_not_exists => TRUE
);

-- News items: 30 days is sufficient for sentiment analysis context
SELECT create_hypertable(
    'news_items', 'time',
    if_not_exists => TRUE
);

SELECT add_retention_policy(
    'news_items',
    INTERVAL '30 days',
    if_not_exists => TRUE
);

-- AI decisions: keep 180 days for backtesting and strategy review
-- ai_decisions is a regular table (not hypertable); use a pg_cron job instead.
-- Scheduled via migrations/003_compaction.sql for existing clusters.

-- ============================================================
-- 12. Compression Policies for Continuous Aggregates
-- ============================================================
-- Compress 5m and 15m aggregates after 7 days of inactivity (~10x storage saving).
-- candles_1h and candles_daily have fewer rows — not worth the compression overhead.
ALTER MATERIALIZED VIEW candles_5m SET (
    timescaledb.compress = true
);

SELECT add_compression_policy(
    'candles_5m',
    compress_after => INTERVAL '7 days',
    if_not_exists => TRUE
);

ALTER MATERIALIZED VIEW candles_15m SET (
    timescaledb.compress = true
);

SELECT add_compression_policy(
    'candles_15m',
    compress_after => INTERVAL '7 days',
    if_not_exists => TRUE
);
