-- =============================================================================
-- Solana Sniper Bot — Phase 10: Dip-Buy Price Alert Queue
-- =============================================================================
-- Run: psql -U bot -d solana_bot -f migrations/003_price_alerts.sql
-- =============================================================================

CREATE TABLE IF NOT EXISTS price_alerts (
    id                      BIGSERIAL PRIMARY KEY,
    mint                    TEXT NOT NULL,
    symbol                  TEXT,
    strategy_id             TEXT NOT NULL,
    alert_type              TEXT NOT NULL,            -- 'dip_target' | 'dump_from_ath'
    target_price_usd        NUMERIC,
    target_ath_distance_pct NUMERIC,                  -- e.g. -80.0 for "80% below ATH"
    ath_price_seen_usd      NUMERIC,
    detected_at_ms          BIGINT NOT NULL,
    expires_at_ms           BIGINT NOT NULL,
    signal_data             JSONB NOT NULL,           -- snapshot of original candidate
    status                  TEXT DEFAULT 'pending',   -- pending | triggered | expired | cancelled
    triggered_at_ms         BIGINT,
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

-- Fast lookup for the polling loop: only pending rows, by mint.
CREATE INDEX IF NOT EXISTS idx_price_alerts_status_mint
    ON price_alerts(status, mint);

-- Expiry sweep: partial index covering only pending rows.
CREATE INDEX IF NOT EXISTS idx_price_alerts_expires
    ON price_alerts(expires_at_ms)
    WHERE status = 'pending';
