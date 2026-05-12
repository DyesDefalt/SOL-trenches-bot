-- =============================================================================
-- Solana Sniper Bot — Initial Schema
-- =============================================================================
-- Run: psql -U bot -d solana_bot -f migrations/001_initial.sql
-- =============================================================================

CREATE TABLE IF NOT EXISTS positions (
    id              BIGSERIAL PRIMARY KEY,
    token_address   TEXT NOT NULL,
    token_symbol    TEXT,
    token_name      TEXT,
    -- Entry
    entry_price_usd NUMERIC(20, 12) NOT NULL,
    entry_amount_sol NUMERIC(20, 9) NOT NULL,  -- SOL spent
    entry_amount_token NUMERIC(40, 12) NOT NULL,  -- token received
    entry_signature TEXT,                       -- Solana tx signature
    entry_timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    entry_score     NUMERIC(5, 2),              -- score saat entry
    entry_smart_money_count INT,                -- snapshot
    -- Position state
    status          TEXT NOT NULL DEFAULT 'OPEN',  -- OPEN | CLOSED | LIQUIDATED | ERROR
    peak_price_usd  NUMERIC(20, 12),            -- untuk trailing stop
    -- Exit
    exit_price_usd  NUMERIC(20, 12),
    exit_amount_sol NUMERIC(20, 9),             -- SOL received
    exit_signature  TEXT,
    exit_timestamp  TIMESTAMPTZ,
    exit_reason     TEXT,                       -- TP1 | TP2 | TP3 | SL | TRAILING | TIME_EXIT | MANUAL | CB_EMERGENCY
    -- Realized PnL
    realized_pnl_sol NUMERIC(20, 9),
    realized_pnl_pct NUMERIC(10, 4),
    -- Meta
    dry_run         BOOLEAN NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_token ON positions(token_address);
CREATE INDEX IF NOT EXISTS idx_positions_entry_ts ON positions(entry_timestamp DESC);


-- Per-position scaling out (TP staircase) tracking
CREATE TABLE IF NOT EXISTS position_partial_exits (
    id              BIGSERIAL PRIMARY KEY,
    position_id     BIGINT NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
    tier            TEXT NOT NULL,              -- TP1 | TP2 | TP3 | TRAILING
    sell_price_usd  NUMERIC(20, 12) NOT NULL,
    sell_amount_token NUMERIC(40, 12) NOT NULL,
    sell_amount_sol NUMERIC(20, 9) NOT NULL,
    signature       TEXT,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    pnl_pct         NUMERIC(10, 4)
);

CREATE INDEX IF NOT EXISTS idx_partial_exits_position ON position_partial_exits(position_id);


-- Trade signals log — semua scoring decision (BUY/ALERT/SKIP/REJECT)
CREATE TABLE IF NOT EXISTS signals (
    id              BIGSERIAL PRIMARY KEY,
    token_address   TEXT NOT NULL,
    token_symbol    TEXT,
    score           NUMERIC(5, 2) NOT NULL,
    action          TEXT NOT NULL,              -- BUY | ALERT | SKIP | REJECT
    reject_reasons  JSONB,                      -- list of strings
    breakdown       JSONB NOT NULL,             -- ScoreBreakdown
    context         JSONB NOT NULL,             -- TokenData snapshot
    smart_money_count INT,
    smart_money_buyers JSONB,                   -- list of wallet addresses
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_signals_action_ts ON signals(action, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_signals_token ON signals(token_address);


-- Smart wallet snapshot — periodic refresh dari registry untuk audit
CREATE TABLE IF NOT EXISTS smart_wallet_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    address         TEXT NOT NULL,
    tier            TEXT NOT NULL,
    winrate         NUMERIC(5, 4),
    realized_profit NUMERIC(20, 9),
    buy_count       INT,
    sell_count      INT,
    source          TEXT NOT NULL,              -- auto | manual | blacklist
    snapshot_time   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sw_snapshot_addr ON smart_wallet_snapshots(address, snapshot_time DESC);


-- Circuit breaker events
CREATE TABLE IF NOT EXISTS circuit_breaker_events (
    id              BIGSERIAL PRIMARY KEY,
    trigger_type    TEXT NOT NULL,              -- CONSECUTIVE_LOSS | DAILY_LOSS | DRAWDOWN | etc
    threshold_value NUMERIC(20, 4),
    actual_value    NUMERIC(20, 4),
    paused_until    TIMESTAMPTZ,
    state_snapshot  JSONB,                      -- positions, balance, recent trades, etc
    resolved_at     TIMESTAMPTZ,
    resolved_by     TEXT,                       -- 'auto' | 'manual:user_id' | etc
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cb_events_ts ON circuit_breaker_events(timestamp DESC);


-- Daily PnL aggregate (untuk PnL Calendar)
CREATE TABLE IF NOT EXISTS daily_pnl (
    date            DATE PRIMARY KEY,
    trades_total    INT NOT NULL DEFAULT 0,
    trades_won      INT NOT NULL DEFAULT 0,
    trades_lost     INT NOT NULL DEFAULT 0,
    pnl_sol         NUMERIC(20, 9) NOT NULL DEFAULT 0,
    fees_sol        NUMERIC(20, 9) NOT NULL DEFAULT 0,
    starting_balance_sol NUMERIC(20, 9),
    ending_balance_sol NUMERIC(20, 9),
    largest_win_sol NUMERIC(20, 9),
    largest_loss_sol NUMERIC(20, 9),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- Helper view: open positions
CREATE OR REPLACE VIEW open_positions AS
SELECT
    p.id,
    p.token_address,
    p.token_symbol,
    p.entry_price_usd,
    p.entry_amount_sol,
    p.entry_score,
    p.entry_timestamp,
    p.peak_price_usd,
    EXTRACT(EPOCH FROM (NOW() - p.entry_timestamp)) / 60 AS age_minutes
FROM positions p
WHERE p.status = 'OPEN'
ORDER BY p.entry_timestamp DESC;


-- Trigger: auto update updated_at
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS update_positions_updated_at ON positions;
CREATE TRIGGER update_positions_updated_at
BEFORE UPDATE ON positions
FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
