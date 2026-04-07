-- ============================================================
-- Migration 003: Compaction, retention policies, exit_reason
-- Idempotent — safe to run on existing clusters.
-- ============================================================

-- ── 1. Add exit_reason column to trades (no-op if already present) ──────────
ALTER TABLE trades ADD COLUMN IF NOT EXISTS exit_reason TEXT;

-- ── 2. Reduce market_candles retention: 90 days → 30 days ───────────────────
-- Raw 1-min candles older than 30 days are covered by the 5m/15m/1h/daily
-- continuous aggregates.  Trimming to 30 days prevents unbounded growth.
-- remove_retention_policy is wrapped in a DO block because some TimescaleDB
-- builds don't support the named if_not_exists parameter.
DO $$
BEGIN
    PERFORM remove_retention_policy('market_candles');
EXCEPTION WHEN others THEN
    -- no existing policy — nothing to remove
END $$;

SELECT add_retention_policy(
    'market_candles',
    INTERVAL '30 days',
    if_not_exists => TRUE
);

-- ── 3. Retention for continuous aggregate views (only if they exist) ─────────
DO $$
BEGIN
    IF to_regclass('candles_5m') IS NOT NULL THEN
        PERFORM add_retention_policy('candles_5m',  INTERVAL '90 days',  if_not_exists => TRUE);
    END IF;
    IF to_regclass('candles_15m') IS NOT NULL THEN
        PERFORM add_retention_policy('candles_15m', INTERVAL '180 days', if_not_exists => TRUE);
    END IF;
    IF to_regclass('candles_1h') IS NOT NULL THEN
        PERFORM add_retention_policy('candles_1h',  INTERVAL '365 days', if_not_exists => TRUE);
    END IF;
END $$;

-- ── 4. Retention for news_items (30 days) ────────────────────────────────────
SELECT add_retention_policy(
    'news_items',
    INTERVAL '30 days',
    if_not_exists => TRUE
);

-- ── 5. Retention for ai_decisions (180 days) ─────────────────────────────────
-- ai_decisions is a regular (non-hypertable) table; use a scheduled DELETE.
CREATE OR REPLACE FUNCTION purge_old_decisions() RETURNS void
LANGUAGE sql AS $$
    DELETE FROM ai_decisions
    WHERE time < NOW() - INTERVAL '180 days';
$$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_cron') THEN
        PERFORM cron.schedule(
            'purge-old-decisions',
            '30 20 * * *',
            'SELECT purge_old_decisions()'
        );
    END IF;
END $$;

-- ── 6. Compression for continuous aggregates (only if they exist) ─────────────
DO $$
BEGIN
    IF to_regclass('candles_5m') IS NOT NULL THEN
        EXECUTE 'ALTER MATERIALIZED VIEW candles_5m SET (timescaledb.compress = true)';
        PERFORM add_compression_policy('candles_5m',  compress_after => INTERVAL '7 days', if_not_exists => TRUE);
    END IF;
    IF to_regclass('candles_15m') IS NOT NULL THEN
        EXECUTE 'ALTER MATERIALIZED VIEW candles_15m SET (timescaledb.compress = true)';
        PERFORM add_compression_policy('candles_15m', compress_after => INTERVAL '7 days', if_not_exists => TRUE);
    END IF;
END $$;
