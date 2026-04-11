-- Migration 006: create options_oi_snapshots hypertable
-- Intraday time-series of options chain OI, captured every 5 minutes.
-- One row per (time, symbol, expiry, strike, option_type).
CREATE TABLE IF NOT EXISTS options_oi_snapshots (
    time        TIMESTAMPTZ     NOT NULL,
    symbol      TEXT            NOT NULL,
    expiry      DATE            NOT NULL,
    strike      INTEGER         NOT NULL,
    option_type TEXT            NOT NULL CHECK (option_type IN ('CE', 'PE')),
    ltp         NUMERIC(12, 2),
    oi          BIGINT,
    oi_change   BIGINT,
    volume      BIGINT
);

SELECT create_hypertable(
    'options_oi_snapshots', 'time',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE UNIQUE INDEX IF NOT EXISTS options_oi_unique_idx
    ON options_oi_snapshots (time, symbol, expiry, strike, option_type);

CREATE INDEX IF NOT EXISTS options_oi_symbol_expiry_strike_idx
    ON options_oi_snapshots (symbol, expiry, strike, time DESC);

SELECT add_retention_policy(
    'options_oi_snapshots',
    INTERVAL '90 days',
    if_not_exists => TRUE
);
