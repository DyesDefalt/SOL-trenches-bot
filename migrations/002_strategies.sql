-- =============================================================================
-- Solana Sniper Bot — Phase 10: Hot-Reloadable Strategy Manager
-- =============================================================================
-- Run: psql -U bot -d solana_bot -f migrations/002_strategies.sql
-- =============================================================================

CREATE TABLE IF NOT EXISTS strategies (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    enabled     BOOLEAN NOT NULL DEFAULT FALSE,
    config      JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_strategies_enabled ON strategies(enabled);


-- Auto-update updated_at (reuse the function defined in 001_initial.sql)
DROP TRIGGER IF EXISTS update_strategies_updated_at ON strategies;
CREATE TRIGGER update_strategies_updated_at
BEFORE UPDATE ON strategies
FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();


-- =============================================================================
-- Enforce: at most 1 strategy enabled=true at a time
-- =============================================================================
CREATE OR REPLACE FUNCTION enforce_single_active_strategy()
RETURNS TRIGGER AS $$
BEGIN
    -- Only fire when enabling a strategy
    IF NEW.enabled = TRUE AND (OLD IS NULL OR OLD.enabled = FALSE) THEN
        UPDATE strategies
           SET enabled = FALSE,
               updated_at = NOW()
         WHERE enabled = TRUE
           AND id <> NEW.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_single_active_strategy ON strategies;
CREATE TRIGGER trg_single_active_strategy
BEFORE INSERT OR UPDATE ON strategies
FOR EACH ROW EXECUTE FUNCTION enforce_single_active_strategy();


-- =============================================================================
-- Seed: 4 default strategies
-- Only `balanced` is enabled by default.
-- =============================================================================
INSERT INTO strategies (id, name, enabled, config) VALUES

('conservative', 'Conservative', FALSE, '{
    "min_score_to_buy": 85,
    "max_position_size_sol": 0.03,
    "max_concurrent_positions": 1,
    "filter_max_mcap_usd": 40000,
    "filter_min_liquidity_usd": 12000,
    "filter_min_gmgn_security_score": 80,
    "tp1_gain_pct": 60,
    "tp1_sell_pct": 35,
    "tp2_gain_pct": 120,
    "tp2_sell_pct": 35,
    "tp3_gain_pct": 250,
    "tp3_sell_pct": 20,
    "hard_sl_pct": -30,
    "trailing_stop_pct": 20,
    "time_based_exit_minutes": 30,
    "slippage_bps": 1000,
    "entry_mode": "wait_for_dip",
    "max_ath_distance_pct": -5,
    "score_weight_smart_money": 40,
    "score_weight_security": 15
}'::jsonb),

('balanced', 'Balanced', TRUE, '{
    "min_score_to_buy": 75,
    "max_position_size_sol": 0.05,
    "max_concurrent_positions": 2,
    "filter_max_mcap_usd": 60000,
    "filter_min_liquidity_usd": 8000,
    "filter_min_gmgn_security_score": 70,
    "tp1_gain_pct": 80,
    "tp1_sell_pct": 30,
    "tp2_gain_pct": 150,
    "tp2_sell_pct": 30,
    "tp3_gain_pct": 300,
    "tp3_sell_pct": 25,
    "hard_sl_pct": -45,
    "trailing_stop_pct": 30,
    "time_based_exit_minutes": 45,
    "slippage_bps": 1500,
    "entry_mode": "immediate",
    "max_ath_distance_pct": -10,
    "score_weight_smart_money": 35,
    "score_weight_security": 10
}'::jsonb),

('aggressive', 'Aggressive', FALSE, '{
    "min_score_to_buy": 65,
    "max_position_size_sol": 0.10,
    "max_concurrent_positions": 4,
    "filter_max_mcap_usd": 100000,
    "filter_min_liquidity_usd": 5000,
    "filter_min_gmgn_security_score": 60,
    "tp1_gain_pct": 100,
    "tp1_sell_pct": 25,
    "tp2_gain_pct": 200,
    "tp2_sell_pct": 25,
    "tp3_gain_pct": 500,
    "tp3_sell_pct": 30,
    "hard_sl_pct": -60,
    "trailing_stop_pct": 40,
    "time_based_exit_minutes": 60,
    "slippage_bps": 2000,
    "entry_mode": "immediate",
    "max_ath_distance_pct": -15,
    "score_weight_smart_money": 30,
    "score_weight_security": 8
}'::jsonb),

('dip_buy', 'Dip Buy', FALSE, '{
    "min_score_to_buy": 70,
    "max_position_size_sol": 0.06,
    "max_concurrent_positions": 3,
    "filter_max_mcap_usd": 80000,
    "filter_min_liquidity_usd": 10000,
    "filter_min_gmgn_security_score": 72,
    "tp1_gain_pct": 90,
    "tp1_sell_pct": 33,
    "tp2_gain_pct": 180,
    "tp2_sell_pct": 33,
    "tp3_gain_pct": 350,
    "tp3_sell_pct": 25,
    "hard_sl_pct": -40,
    "trailing_stop_pct": 25,
    "time_based_exit_minutes": 50,
    "slippage_bps": 1200,
    "entry_mode": "wait_for_dump",
    "max_ath_distance_pct": -20,
    "score_weight_smart_money": 38,
    "score_weight_security": 12
}'::jsonb)

ON CONFLICT (id) DO NOTHING;
