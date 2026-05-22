-- =============================================================================
-- Phase 11.4 — Add `trench_low_mcap` strategy (Meridian community wisdom)
-- =============================================================================
-- "main di mcap 7-15k rrnya lebih bagus daripada 50k keatas" — Meridian admin
-- Per Petsreid/badidoyo/Ponyin: best entries are sub-15k MC, before crowd discovers
--
-- Run: psql -U bot -d solana_bot -f migrations/004_trench_low_mcap.sql
-- =============================================================================

INSERT INTO strategies (id, name, enabled, config, created_at, updated_at)
VALUES (
    'trench_low_mcap',
    'Trench Low MC (7-15k sweet spot)',
    FALSE,
    jsonb_build_object(
        'min_score_to_buy', 70,
        'min_score_to_alert', 60,
        'max_position_size_sol', 0.030,
        'max_concurrent_positions', 3,
        'filter_min_mcap_usd', 5000,
        'filter_max_mcap_usd', 18000,
        'filter_min_liquidity_usd', 6000,
        'filter_min_gmgn_security_score', 70,
        'filter_max_dev_holding_pct', 10,
        'filter_max_bundle_supply_pct', 20,
        'entry_mode', 'immediate',
        'tp1_gain_pct', 80,
        'tp1_sell_pct', 25,
        'tp2_gain_pct', 200,
        'tp2_sell_pct', 30,
        'tp3_gain_pct', 500,
        'tp3_sell_pct', 25,
        'hard_sl_pct', -30,
        'trailing_stop_pct', 25,
        'time_based_exit_minutes', 60,
        'slippage_bps', 1800,
        'score_weight_smart_money', 35,
        'score_weight_security', 12,
        'score_weight_mcap_position', 22,
        'score_weight_volume_momentum', 15,
        'score_weight_liquidity', 10,
        'score_weight_kol_social', 6,
        'notes', 'Sub-15k MC sweet spot per Meridian/Petsreid/badidoyo community wisdom. Smaller positions, bigger TPs, tighter SL.'
    ),
    NOW(),
    NOW()
)
ON CONFLICT (id) DO UPDATE
    SET name = EXCLUDED.name,
        config = EXCLUDED.config,
        updated_at = NOW();
