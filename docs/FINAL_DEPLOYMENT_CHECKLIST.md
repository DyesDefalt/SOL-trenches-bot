# Final Deployment Checklist — 100% Complete

Status: **All phases done.** This is the go-live checklist.

## Stack Summary

| Layer | Phase | Status |
|---|---|---|
| Infra setup docs | 0 | ✅ Doc ready |
| Data clients (Helius, GMGN, GeckoTerminal) | 1 | ✅ |
| Smart wallet registry (3-layer) | 1 | ✅ |
| Backtester + decision gate | 2 | ✅ |
| Live signal pipeline | 3 | ✅ |
| Execution + position + circuit breaker + DB | 4 | ✅ |
| Telegram bot + main orchestrator | 5 | ✅ |
| AI agent stack (rug check + reflection + wallet + tuner) | 6 | ✅ |
| Multi-source intelligence (Nansen+Birdeye+Rugcheck+DexScreener+Pumpfun) | 7 | ✅ |
| GMGN swap alternative | 7g | ✅ |
| Production polish (health endpoint, Prometheus, systemd watchdog) | 8 | ✅ |
| Extended intelligence (CryptoQuant+AlphaVantage+CryptoPanic+Messari+CoinGecko+Tokito) | 9 | ✅ |
| Charon parity (hot-reload strategies, fee-claim WS, dip-buy mode, interactive menus) | 10 | ✅ |
| Trader filters bundle (anti-bundler + global fee + funded-from + holder balance) | 10.5 | ✅ |
| AI Meme Quality Scorer + Fibonacci 0.786 entry helper | 10.6 | ✅ |
| Operator UX polish (quick-action buttons, rich /pnl breakdown, rich position cards, trench_low_mcap) | 11 | ✅ |

**Total: 514 unit tests pass.**

## Pre-Flight Checklist

### Infrastructure (Phase 0)

- [ ] Tencent Cloud Singapore VPS aktif, SSH bekerja
- [ ] User `bot` non-root, SSH key auth only
- [ ] UFW firewall aktif, hanya port 22 (SSH) terbuka
- [ ] IPv6 disabled (untuk kompatibilitas GMGN)
- [ ] Python 3.11+ installed
- [ ] PostgreSQL 15 running + database `solana_bot` created
- [ ] Redis running + accessible
- [ ] Node.js 20+ installed
- [ ] `gmgn-cli` global installed (`npm install -g gmgn-cli`)
- [ ] `nansen-cli` global installed (kalau pakai Nansen)

### Credentials (all in password manager + secrets/.env)

- [ ] Helius API key obtained (free tier OK)
- [ ] GMGN Ed25519 keypair generated, public uploaded to gmgn.ai/ai, API key saved
- [ ] Solana hot wallet baru generated, seed phrase backup OFFLINE
- [ ] 0.36 SOL transferred to bot wallet, confirmed di Solscan
- [ ] Telegram bot created via BotFather, token + chat ID saved
- [ ] (Optional) Nansen API key — sign up at app.nansen.ai
- [ ] (Optional) Birdeye API key
- [ ] (Optional) OpenRouter API key — sign up at openrouter.ai, top up $5

### Phase 9 Extended Intelligence (Optional — recommended for production)

- [ ] CryptoQuant API key — https://cryptoquant.com/settings/api (free tier limited, $29/mo Pro recommended)
- [ ] Alpha Vantage API key — https://www.alphavantage.co/support/#api-key (free tier 25 req/day)
- [ ] CryptoPanic API key — https://cryptopanic.com/developers/api/about (free public)
- [ ] Messari API key — https://messari.io/account/api (free 20 req/min)
- [ ] CoinGecko Demo API key — https://www.coingecko.com/en/developers/dashboard (free 10k/mo)
- [ ] Tokito API key — alternative LLM provider (OpenAI-compatible)
- [ ] Run `make phase9-smoke` after entering keys — verify all 6 services respond

### Code Deployment

- [ ] Code cloned/extracted ke `/home/bot/solana-bot`
- [ ] `make install-dev` ran successfully
- [ ] `cp .env.example secrets/.env && chmod 600 secrets/.env`
- [ ] All credentials filled in `secrets/.env`
- [ ] `secrets/bot-wallet.json` uploaded with chmod 600
- [ ] `secrets/gmgn_private.pem` uploaded with chmod 600
- [ ] `make db-init` ran successfully
- [ ] `make db-migrate-phase10` ran successfully (strategies + price_alerts tables seeded)
- [ ] `make db-migrate-phase11` ran successfully (trench_low_mcap strategy + position override columns)
- [ ] `make list-strategies` shows 5 strategies (conservative, balanced=active, aggressive, dip_buy, trench_low_mcap)

### Smoke Tests

- [ ] `make smoke` — semua 11 source pass (Helius, GMGN, GeckoTerminal, Telegram, Redis, Postgres, Nansen, Birdeye, Rugcheck, DexScreener, Pump.fun)
- [ ] `make intel-smoke` — Phase 7 intel layer end-to-end works
- [ ] `make phase9-smoke` — Phase 9 extended intel (6 sources + 3 aggregators)
- [ ] `make bootstrap-wallets` — registry populated
- [ ] `make stats-wallets` — verify A+B tier wallets >= 20

### Decision Gate (Phase 2 Backtest)

- [ ] `make backtest` ran with `--sample 50`
- [ ] Output: win_rate >= 40%, profit_factor >= 1.5, max_drawdown <= 50%, total_return >= 15%, trade_count >= 25
- [ ] Gate PASSED — strategy valid

### Live Deployment

- [ ] `secrets/.env` shows `DRY_RUN=true` (FIRST)
- [ ] `make deploy` ran successfully (systemd service installed)
- [ ] `sudo systemctl status solana-bot` → active (running)
- [ ] Telegram received "Bot started" message
- [ ] `/status` command responds correctly
- [ ] Wait 24-48 jam observe behavior dengan DRY_RUN
- [ ] Audit DB: `SELECT action, COUNT(*) FROM signals GROUP BY action;`
- [ ] Errors di log < 10 per 24 jam, no critical
- [ ] Circuit breaker tidak triggered berlebihan

### Production Monitoring (Phase 8)

- [ ] Health endpoint: `curl http://localhost:8080/health` → 200 OK
- [ ] Metrics endpoint: `curl http://localhost:8080/metrics` → Prometheus format
- [ ] Systemd watchdog active (`systemctl show solana-bot | grep WatchdogSec`)

### Live Trading Switch

After 24-48h DRY_RUN smooth:

- [ ] Stop bot: `sudo systemctl stop solana-bot`
- [ ] Edit `secrets/.env`: `DRY_RUN=false`
- [ ] Start bot: `sudo systemctl start solana-bot`
- [ ] Telegram should show "💵 LIVE" instead of "🧪 DRY"
- [ ] Monitor pertama LIVE BUY di Solscan, verify TX
- [ ] Watch closely 1 minggu pertama

### AI Layer Enablement (Phase 6, Optional)

After bot stable 1 minggu LIVE:

- [ ] OpenRouter API key obtained + top up
- [ ] Edit `.env`: `AI_ENABLED=true`, `AI_RUG_CHECK_ENABLED=true`, `AI_REFLECTION_ENABLED=true`
- [ ] Restart bot
- [ ] Monitor 3-5 hari, check `data/lessons.json` accumulating
- [ ] Check daily cost in log < $0.50/hari

### Weekly Tuner (Phase 6c, Optional)

After bot stable 30 hari LIVE with AI:

- [ ] Edit `.env`: `AI_TUNER_ENABLED=true`
- [ ] Add cron: `0 3 * * 1 cd /home/bot/solana-bot && venv/bin/python scripts/run_weekly_tuner.py >> logs/tuner.log 2>&1`
- [ ] First Monday: receive Telegram recommendation
- [ ] Manual apply via `/applyTuning param value` (max ±20% from current)

## Daily Operations

### Morning Routine (~5 min)

```bash
ssh bot@<VPS_IP>
cd ~/solana-bot

# Status
sudo systemctl is-active solana-bot
curl -s http://localhost:8080/health | jq

# Errors last 24h
sudo journalctl -u solana-bot --since '24 hours ago' | grep -iE 'error|critical' | head

# PnL yesterday
psql -U bot -d solana_bot -c "SELECT * FROM daily_pnl ORDER BY date DESC LIMIT 3;"

# Open positions
psql -U bot -d solana_bot -c "SELECT * FROM open_positions;"
```

### Weekly Review (~30 min)

```sql
-- Trade stats this week
SELECT 
  COUNT(*) total_trades,
  COUNT(*) FILTER (WHERE realized_pnl_sol > 0) wins,
  ROUND(AVG(realized_pnl_pct)::numeric, 2) avg_pnl_pct,
  ROUND(SUM(realized_pnl_sol)::numeric, 4) total_pnl_sol
FROM positions 
WHERE exit_timestamp >= NOW() - INTERVAL '7 days' AND status = 'CLOSED';
```

## Phase 10 Operations (Hot-Reload Strategies + New Telegram)

### Switching Strategies Without Restart

Via Telegram (no SSH needed):
```
/strategy                          # list all 4 strategies with active marker
/strategy dip_buy                  # activate dip_buy (or balanced/aggressive/conservative)
/stratset dip_buy tp1_gain_pct 50  # change one param, applies on next signal cycle
/menu                              # open interactive inline-keyboard menu
```

Via SQL:
```sql
-- Show active strategy + its full config
SELECT id, name, enabled, config FROM strategies WHERE enabled = TRUE;

-- Update a single param (replaces JSONB key)
UPDATE strategies 
SET config = jsonb_set(config, '{hard_sl_pct}', '-20'::jsonb), 
    updated_at = NOW() 
WHERE id = 'balanced';
```

### Monitoring Phase 10 New Signal Sources

```sql
-- Fee-claim events received in last 24h
SELECT COUNT(*) FROM signals 
WHERE context->>'fee_claim_signal' = 'true' 
  AND timestamp >= NOW() - INTERVAL '24 hours';

-- Pending dip-buy price alerts
SELECT mint, symbol, strategy_id, alert_type, 
       target_ath_distance_pct, ROUND((expires_at_ms - EXTRACT(EPOCH FROM NOW())*1000) / 60000) AS minutes_remaining
FROM price_alerts 
WHERE status = 'pending' 
ORDER BY detected_at_ms DESC;

-- Trader filter rejections (anti-bundler veto stats)
SELECT 
  context->>'bundler_pattern_strength' AS bundler_strength,
  context->>'fee_analysis_label' AS fee_label,
  COUNT(*)
FROM signals 
WHERE action = 'REJECT' 
  AND timestamp >= NOW() - INTERVAL '24 hours'
GROUP BY 1, 2;
```

## Reference Docs

- `docs/phase-0-setup.md` — Initial VPS + account setup
- `docs/PHASE_7_QUICK_REFERENCE.md` — Multi-source intel layer reference
- `README.md` — Project overview + quick start + Phase 10 details
- This file — Final deployment checklist

## Emergency Procedures

### Pause Trading

Via Telegram: `/pause`
Via SSH: `sudo systemctl stop solana-bot`

### Withdraw All SOL to Cold Wallet

```bash
# Generate cold wallet di LAPTOP (jangan di VPS)
solana-keygen new --outfile cold-wallet.json

# Di VPS
solana config set --keypair ~/solana-bot/secrets/bot-wallet.json
solana transfer <COLD_ADDRESS> ALL --allow-unfunded-recipient \
  --url https://api.mainnet-beta.solana.com
```

### Audit Suspicious Activity

```sql
-- Recent trades
SELECT * FROM positions ORDER BY created_at DESC LIMIT 20;

-- Recent signals (BUY/ALERT only)
SELECT * FROM signals WHERE action IN ('BUY', 'ALERT') 
  ORDER BY timestamp DESC LIMIT 20;

-- Circuit breaker events
SELECT * FROM circuit_breaker_events 
  ORDER BY timestamp DESC LIMIT 10;
```

## Cost Summary (May 2026)

| Component | Cost/month |
|---|---|
| Tencent SG VPS (annual promo) | $0.84 |
| Helius RPC (Free tier) | $0 |
| GMGN API (Free tier) | $0 |
| Birdeye (Free tier) | $0 |
| Rugcheck (Public) | $0 |
| DexScreener (Public) | $0 |
| Pump.fun (Public) | $0 |
| Nansen Pro (optional Phase 7) | $99 |
| OpenRouter LLM (Phase 6, optional) | ~$1 |
| CryptoQuant Pro (Phase 9, optional) | $29 |
| Alpha Vantage (Phase 9 free tier) | $0 |
| CryptoPanic (Phase 9 public) | $0 |
| Messari (Phase 9 free tier) | $0 |
| CoinGecko Demo (Phase 9 free tier) | $0 |
| Tokito (Phase 9 alt LLM, pay-per-token) | ~$1-3 |
| **Total minimum (Phase 1-5 + 7 + 9 free-tier)** | **~$1-5** |
| **Total recommended (free + OpenRouter + CryptoQuant Pro)** | **~$31** |
| **Total maximum (Nansen Pro + everything)** | **~$135** |

## Disclaimer

Trading memecoin sangat berisiko. 82-90% retail sniper user RUGI long-term per Pump.fun on-chain data. Bot ini untuk learning + experimentation. **Tidak ada jaminan profit.** Modal 0.36 SOL untuk test mekanik + validasi strategi, bukan profit signifikan.
