# Solana Sniper / Trencher Bot

Bot autonomous untuk Solana memecoin sniping dengan smart-money tracking, scoring engine, circuit breaker, position management, dan Telegram interface. Modal awal 0.36 SOL (~Rp 500k), all-FREE-tier infrastructure (~$1-5/bulan).

## Status: 100% COMPLETE — Ready for Backtest + Live Test + AI Augmentation

| Phase | Status | LOC | Tests |
|---|---|---|---|
| 0 — Setup Infra | ✅ Doc ready | — | — |
| 1 — Data Layer + Smart Wallet Registry | ✅ Complete | ~2.5k | 34 |
| 2 — Backtester + Scoring Engine + Decision Gate | ✅ Complete | ~1.8k | 16 |
| 3 — Live Signal (Scanner + Tracker + Signal Engine) | ✅ Complete | ~1k | — |
| 4 — Execution + Position Manager + Circuit Breaker + DB | ✅ Complete | ~2.5k | 9 |
| 5 — Telegram Bot + Main Orchestrator + Deploy | ✅ Complete | ~1.5k | — |
| **6 — AI Agent Stack (Rug Check + Reflection + Wallet + Tuner)** | **✅ Complete** | **~1.5k** | **28** |
| **7 — Multi-Source Intelligence (5 sources + verifier + cluster)** | **✅ Complete** | **~3k** | **76** |
| **7g — GMGN Swap Alternative Execution** | **✅ Complete** | **~270** | **6** |
| **8 — Production Polish (Health + Prometheus + Watchdog)** | **✅ Complete** | **~250** | **10** |

**179 unit tests pass.** Backtester verified end-to-end. Multi-source intel + AI advisor + production observability all wired.

## Phase 7: Multi-Source Intelligence (NEW)

Bot now integrates 5 external data sources for richer signal:

| Source | Use For | Cost |
|---|---|---|
| **Nansen** | Smart money trend, accumulation pattern, labels (Fund/Smart Trader) | Pro tier $99/mo or x402 micropayment |
| **GMGN** | Cluster signal detection (3+ wallets same token in 30min), token security | Free |
| **Rugcheck** | Safety scoring, honeypot detection, LP lock status | Free (public) |
| **DexScreener** | Price, liquidity, volume across all DEXs | Free (public) |
| **Birdeye** | Token overview, holder breakdown, security flags | Free tier (premium for advanced) |
| **Pump.fun** | Bonding curve, graduation tracking (70-95% sweet spot) | Free (public) |

### Phase 7 Modules

```
src/intel/
├── nansen_client.py        # Nansen Token God Mode + Smart Money API
├── birdeye_client.py       # Birdeye free tier
├── rugcheck_client.py      # Rugcheck safety scoring
├── dexscreener_client.py   # DexScreener price/liquidity
├── pumpfun_client.py       # Pump.fun bonding curve
├── smart_money.py          # Unified Nansen + GMGN aggregator
├── cluster_detector.py     # 3+ wallets in 30min detector
├── token_verifier.py       # 5-source voting safety check
└── pumpfun_tracker.py      # Graduation status tracker
```

### Setup (additional after Phase 0)

```bash
# Install Node.js 20+ for gmgn-cli and nansen-cli
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs
sudo npm install -g gmgn-cli nansen-cli

# Edit secrets/.env to add:
# NANSEN_API_KEY=...
# BIRDEYE_API_KEY=...  (optional)

# Run smoke test (includes Phase 7 sources)
make smoke
```

## Critical Facts

- **Modal:** 0.36 SOL (~$31 USD @ $87/SOL May 2026)
- **Stack cost:** ~$1-5/mo (Tencent SG VPS, Helius free, GMGN free, GeckoTerminal free)
- **Anti-MEV:** Helius Sender (gratis, parallel route ke Jito + Helius)
- **Reality check:** 82-90% sniper user lose money; modal 0.36 SOL untuk learning + test mekanik

## Decision Gates

### Backtest gate (sebelum live)
- Win rate ≥ 40%, profit factor ≥ 1.5, max drawdown ≤ 50%, total return ≥ 15%, min 25 trades

### Circuit breaker (decoupled watchdog)
- 3 consecutive losses → pause 6h
- Daily loss -30% → pause 24h
- Weekly loss -50% → halt manual review
- Max drawdown -50% from peak → emergency stop
- Win rate <25% rolling 20 trades → pause manual
- Drawdown velocity -20% in <1h → emergency stop

## Quick Start

### Setelah Phase 0 selesai (sudah ada Tencent VPS, Helius/GMGN keys, Solana wallet, Telegram bot)

```bash
# 1. Clone + setup
git clone <repo> solana-bot
cd solana-bot
make install-dev

# 2. Setup credential
cp .env.example secrets/.env
chmod 600 secrets/.env
# Edit secrets/.env: HELIUS_API_KEY, GMGN_API_KEY, TELEGRAM_*, etc.

# 3. Generate wallet + GMGN keypair
make wallet-gen
make gmgn-keygen
# Upload public key ke gmgn.ai/ai, simpan API key di .env

# 4. Init DB schema
make db-init

# 5. Verify konektivitas semua API
make smoke

# 6. Bootstrap smart wallet registry
make bootstrap-wallets
make stats-wallets

# 7. Run BACKTEST (CRITICAL — sebelum live)
make backtest
# Hasil: data/backtest_results/run_<timestamp>.json
# Decision gate evaluation di console

# 8. Kalau gate PASSED, run bot dalam DRY_RUN mode
# Pastikan DRY_RUN=true di .env
make run
# Atau install systemd service (production):
make deploy
sudo systemctl start solana-bot
sudo journalctl -u solana-bot -f

# 9. Production observability (Phase 8)
make health-check                  # /health endpoint
make metrics                       # Prometheus metrics
curl http://localhost:8080/ready   # k8s-style ready probe

# 10. (Optional) Enable AI Agent Layer (Phase 6, after 1 minggu stable)
# Edit .env: AI_ENABLED=true, AI_RUG_CHECK_ENABLED=true, AI_REFLECTION_ENABLED=true
# OPENROUTER_API_KEY=sk-or-...
sudo systemctl restart solana-bot
make ai-cost                       # check daily LLM spend

# 11. (Optional) Weekly Tuner (Phase 6c)
# Add cron: 0 3 * * 1 cd ~/solana-bot && make tuner-run >> logs/tuner.log 2>&1
make tuner-run                     # manual test first
```

## Smart Wallet Discovery — 3 Lapis

**Lapis 1: Auto-discovery dari GMGN (refresh tiap 6 jam)**
- Fetch 200 recent smart-money + KOL trades
- Extract unique wallets, classify tier (A: WR≥65% + profit≥30 SOL, B: WR≥55%, C: WR≥45%)
- Persisted ke `data/smart_wallets.json`

**Lapis 2: Manual additions (override auto)**
```bash
python scripts/manage_smart_wallets.py add <ADDRESS> --tier A --notes "alpha guy from twitter"
```
Atau via Telegram: `/addwallet ADDRESS A "notes"`

**Lapis 3: Blacklist**
```bash
python scripts/manage_smart_wallets.py blacklist <ADDRESS> --notes "wash trader"
```
Atau Telegram: `/blacklist ADDRESS`

## Scoring Engine — Formula 0-100

| Komponen | Bobot | Logic |
|---|---|---|
| Smart Money Count | 35 pts | min(count × 12, 35) — 3 wallet → max |
| MCAP & "Sedang di Bawah" | 20 pts | 60% low MCAP component + 40% below-ATH |
| Volume & Momentum | 15 pts | 80% threshold + 20% increasing flag |
| Liquidity | 10 pts | Linear scale dari min ke 20k |
| Security | 10 pts | 50% GMGN score + 25% LP burned + 25% renounced |
| KOL/Social | 5 pts | per KOL bonus |
| Bundle Penalty | -10 pts | scale 5%-25% bundle supply |

**Threshold:** Score ≥ 75 → BUY, 65-74 → ALERT, < 65 → SKIP

**Hard reject (skip filter):**
- MCAP > $60K, Liquidity < $8K, Honeypot, Dev holding > 15%, Bundle > 30%

## Telegram Commands

- `/status` — bot status, posisi aktif, balance
- `/pnl` — daily PnL 7 hari terakhir
- `/pause` — manual pause trading
- `/resume` — resume after pause
- `/smartlist` — top 10 smart wallets
- `/addwallet ADDRESS [A|B] [notes]` — manual add
- `/blacklist ADDRESS [notes]` — blacklist wallet
- `/config` — current settings
- `/stats` — full stats

## Phase 6: AI Agent Layer (Optional)

LLM advisor stack untuk augment static rules. Default OFF (opt-in via `.env`).

| Agent | When | Purpose | Model |
|---|---|---|---|
| **Rug Check** | Before BUY | Veto edge cases formula miss | Gemini Flash (cheap, high-vol) |
| **Reflection** | After position close | Save lessons, continuous learning | Claude Haiku 4.5 |
| **Wallet Assessor** | New wallet discovery | Detect wash trader / scalper pattern | Claude Haiku 4.5 |
| **Tuner** | Weekly cron | Recommend parameter adjustments | Claude Sonnet 4.6 |

Cost: ~$0.30-1.00/bulan dengan Gemini + Haiku mix (hard cap `LLM_DAILY_COST_CAP_USD=1.00`).

## Phase 8: Production Polish

- **HTTP Health Endpoint** (`:8080/health`, `/ready`, `/metrics`) — for systemd watchdog + Prometheus scraping
- **Prometheus Metrics** — signal cycles, trades, CB trips, LLM cost, latency histograms
- **Graceful Shutdown** — ordered client teardown with timeouts
- **Systemd WatchdogSec=120** — auto-restart kalau bot hang

## Phase 9: Extended Intelligence (Macro + News + Cross-Reference)

Added 6 new data sources to lift the bot from "single-source memecoin sniper" to "multi-layer market intelligence":

### Macro Regime Layer
- **CryptoQuant** — BTC exchange flows, MVRV, funding rates, Coinbase premium
- **Alpha Vantage** — SPX/DXY/VIX proxies (SPY, UUP, VIXY ETFs as TradFi indicators)
- Output: regime enum (`risk_on` / `neutral` / `risk_off` / `extreme_risk_off`)
- **Effect on trading:**
  - `extreme_risk_off` (BTC -10%+ OR DXY surge + SPX dump) → hard skip new entries
  - `risk_off` → position size × 0.5
  - `neutral` → no change
  - `risk_on` (BTC +5% + supportive macro) → position size × 1.3 (capped at max)

### Narrative & News Layer
- **CryptoPanic** — Real-time crypto news with sentiment voting (positive/negative/important)
- **Messari** — Asset profiles + research signals + news feed
- Output: `narrative_bonus` (-10 to +10) + FUD detection
- **Effect on scoring:**
  - Ticker matches trending narrative → +5 score
  - High sentiment (>0.3) → +3
  - Mentioned >5x in 24h → +2
  - **High-severity FUD** (hack/exploit/SEC) → hard reject
  - Medium-severity FUD → -5 to narrative bonus

### Cross-Reference Layer
- **CoinGecko** — Validates token via contract lookup; bonus if listed + ranked
- **Messari** — Cross-check asset profile
- Output: `crossref_bonus` (-5 to +15)
- **Effect on scoring:**
  - Listed on CoinGecko → +5
  - CG market cap rank top 500 → +5 stacking
  - CG top 100 → +10 stacking (rare for memecoin = strong legitimacy signal)
  - Currently trending → +5 additional
  - NOT listed AND age > 7d → -3 (suspicious — established but never indexed)

### Alternative LLM Provider
- **Tokito (pecut-ai)** — OpenAI-compatible endpoint as OpenRouter alternative
- Same `complete_structured()` interface — swap via `LLM_PROVIDER=tokito`
- Use case: cheaper rug check calls, fallback when OpenRouter rate-limits

### Cost Impact
All 6 services have free or near-free tiers:

| Service | Free Tier | Paid Tier |
|---|---|---|
| CryptoQuant | Limited endpoints | $29/mo Pro |
| Alpha Vantage | 25 req/day | $50/mo Premium |
| CryptoPanic | 5 req/sec public | $50/mo Pro |
| Messari | 20 req/min | $24/mo Lite |
| CoinGecko | Demo 10k/mo | $129/mo Analyst |
| Tokito | Pay-per-token | n/a |

**Recommended:** Start with all FREE tiers. CryptoQuant's free tier may 403 some endpoints — handled gracefully. Bot still works with any subset of Phase 9 enabled.

### Scoring Components Total

The engine now has **12 scoring components**:

| Component | Weight | Type |
|---|---|---|
| Smart Money Count | 35 | Additive |
| MCAP & "Below ATH" | 20 | Additive |
| Volume & Momentum | 15 | Additive |
| Liquidity & Fees | 10 | Additive |
| Security (multi-source override) | 10 | Additive |
| KOL/Social | 5 | Additive |
| Bundle/Insider | -10 | Penalty |
| Smart Money Trend (Nansen) | -30 to +30 | Bonus |
| Cluster Signal (GMGN) | 0 to +20 | Bonus |
| Pump.fun Graduation | -5 to +10 | Bonus |
| **Narrative + News** | **-10 to +10** | **Bonus** |
| **Cross-Reference** | **-5 to +15** | **Bonus** |

Plus macro regime as a final position-size multiplier (0.0-1.5x).

### Quick Test

```bash
# Run Phase 9 smoke test (probes all 6 APIs)
make phase9-smoke
```

## Project Structure

```
solana-bot/
├── src/
│   ├── main.py                    # Main orchestrator (asyncio TaskGroup)
│   ├── config.py                  # Pydantic settings
│   ├── clients/
│   │   ├── base.py                # Shared HTTP client (retry, timeout)
│   │   ├── gmgn.py                # GMGN API
│   │   ├── helius.py              # Solana RPC + WebSocket
│   │   ├── helius_sender.py       # Anti-MEV submission (Jito + Helius parallel)
│   │   ├── jupiter.py             # Swap quote + transaction build
│   │   └── geckoterminal.py       # OHLC fallback (free)
│   ├── core/
│   │   ├── scanner.py             # Token candidate discovery
│   │   ├── smart_wallet_registry.py  # 3-layer wallet system
│   │   ├── tracker.py             # Helius WS subscriber
│   │   ├── scoring.py             # Score 0-100 (deterministic)
│   │   ├── signal.py              # Wire scanner+enrich+score
│   │   ├── execution.py           # Buy/sell coordinator
│   │   ├── position.py            # TP staircase, trailing, SL
│   │   └── circuit_breaker.py     # Decoupled watchdog
│   ├── infra/
│   │   ├── cache.py               # Redis (graceful)
│   │   ├── rate_limiter.py        # Leaky/token bucket
│   │   ├── logger.py              # Structlog
│   │   ├── db.py                  # Postgres async
│   │   ├── wallet.py              # Solana keypair manager
│   │   └── telegram.py            # Bot interface + alerts
│   └── backtester/
│       ├── data_fetch.py          # Pull historical OHLCV + cache
│       ├── replay.py              # Replay engine (slippage + fee sim)
│       └── analyze.py             # Decision gate evaluation
├── tests/                         # 72 unit tests pass
├── scripts/
│   ├── test_connections.py        # Smoke test 6 komponen
│   ├── generate_wallet.py         # Solana keypair gen
│   ├── manage_smart_wallets.py    # CLI add/blacklist/list/stats
│   ├── refresh_smart_wallets.py   # Cron refresher
│   └── run_backtest.py            # Backtest runner
├── migrations/
│   └── 001_initial.sql            # Postgres schema
├── deploy/
│   ├── install.sh                 # Production deploy script
│   └── solana-bot.service         # Systemd unit
├── secrets/                       # GITIGNORED
├── data/                          # GITIGNORED (cache, results)
├── logs/                          # GITIGNORED
├── pyproject.toml
├── requirements.txt
├── Makefile
├── .env.example
└── README.md
```

## Production Deployment Workflow

```
[Phase 0 manual setup user]
       ↓
[git clone + make install-dev + setup .env + wallet/key gen]
       ↓
[make smoke] ← verify konektivitas
       ↓
[make bootstrap-wallets] ← populate registry
       ↓
[make backtest] ← DECISION GATE
   ├── PASS → lanjut
   └── FAIL → refine scoring, ulang
       ↓
[DRY_RUN=true di .env]
[make run / make deploy] ← bot jalan paper mode
       ↓
[Monitor 24-48 jam via Telegram alerts]
       ↓
[Audit log + DB, verify behavior reasonable]
       ↓
[DRY_RUN=false di .env]
[sudo systemctl restart solana-bot]
       ↓
[Live test 0.36 SOL, monitor 1 minggu ketat]
       ↓
[30-day verification]
   ├── Profit konsisten → top up modal bertahap
   └── Loss / circuit breaker sering trigger → audit logic, refine
```

## Disclaimer

Trading memecoin sangat berisiko. Bot ini untuk learning + experimentation. **Tidak ada jaminan profit.** Mulai dengan modal kecil yang siap kamu rugikan total. Lakukan due diligence sendiri.

## License

MIT — pakai dengan risiko sendiri.
