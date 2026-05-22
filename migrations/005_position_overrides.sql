-- =============================================================================
-- Phase 11.1 — Per-position override columns for one-tap Telegram actions
-- =============================================================================
-- Allow operator to override TP / SL / trail per individual position via
-- inline keyboard buttons in /menu → Positions → <symbol> → [TP +50%] [SL -15%] etc.
-- Without these columns, all positions share global settings.
--
-- Run: psql -U bot -d solana_bot -f migrations/005_position_overrides.sql
-- =============================================================================

ALTER TABLE positions
    ADD COLUMN IF NOT EXISTS tp_override_pct      NUMERIC(8, 4),
    ADD COLUMN IF NOT EXISTS sl_override_pct      NUMERIC(8, 4),
    ADD COLUMN IF NOT EXISTS trail_disabled       BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS override_set_at_ms   BIGINT,
    ADD COLUMN IF NOT EXISTS override_set_by      TEXT;        -- 'telegram_user_id' OR 'auto'

CREATE INDEX IF NOT EXISTS idx_positions_overrides
    ON positions(status, tp_override_pct, sl_override_pct)
    WHERE status = 'OPEN' AND (tp_override_pct IS NOT NULL OR sl_override_pct IS NOT NULL);
