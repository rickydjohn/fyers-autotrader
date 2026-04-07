-- Migration 004: add broker_order_id to trades for live order tracking
ALTER TABLE trades ADD COLUMN IF NOT EXISTS broker_order_id TEXT;
