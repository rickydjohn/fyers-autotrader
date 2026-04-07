-- ============================================================
-- Migration 002: Multi-year daily OHLCV + Historical S/R levels
-- Apply on existing clusters; init.sql already contains these for fresh ones.
-- Safe to run multiple times (all statements are idempotent).
-- ============================================================

-- Table 1: daily_ohlcv
-- Stores raw daily OHLCV bars pulled from Fyers spanning several years.
-- Intentionally NOT a hypertable and has NO retention policy so data is
-- kept indefinitely — this is the foundation for S/R level computation.
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

-- Table 2: historical_sr_levels
-- Computed support/resistance zones derived from swing-high/low clustering
-- on the multi-year daily chart.  Recomputed on each bootstrap and weekly.
CREATE TABLE IF NOT EXISTS historical_sr_levels (
    id          BIGSERIAL       PRIMARY KEY,
    symbol      TEXT            NOT NULL,
    level       NUMERIC(12, 2)  NOT NULL,
    level_type  TEXT            NOT NULL
                    CHECK (level_type IN ('SUPPORT', 'RESISTANCE', 'BOTH')),
    strength    INTEGER         NOT NULL DEFAULT 1,   -- number of swing-point touches
    first_seen  DATE,
    last_seen   DATE,
    computed_at TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

-- One active level per (symbol, price bucket) — upsert replaces in place
CREATE UNIQUE INDEX IF NOT EXISTS historical_sr_symbol_level_idx
    ON historical_sr_levels (symbol, level);

CREATE INDEX IF NOT EXISTS historical_sr_symbol_strength_idx
    ON historical_sr_levels (symbol, strength DESC);
