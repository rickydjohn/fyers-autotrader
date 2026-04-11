-- Migration 005: add options-related columns to trades for options trade tracking
ALTER TABLE trades ADD COLUMN IF NOT EXISTS option_symbol  VARCHAR(64);
ALTER TABLE trades ADD COLUMN IF NOT EXISTS option_strike  INTEGER;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS option_type    VARCHAR(4);
ALTER TABLE trades ADD COLUMN IF NOT EXISTS option_expiry  VARCHAR(16);
