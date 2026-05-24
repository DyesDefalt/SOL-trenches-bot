.PHONY: help install install-dev test lint format type-check smoke clean run-dev gmgn-keygen wallet-gen bootstrap-wallets refresh-wallets list-wallets stats-wallets backtest backtest-cached run db-init db-migrate-phase10 db-migrate-phase11 list-strategies show-pending-alerts show-overrides deploy phase9-smoke intel-smoke nansen-discover tuner-run health-check metrics ai-cost

help:
	@echo "Solana Sniper Bot — Make targets:"
	@echo ""
	@echo "Setup:"
	@echo "  install              Install runtime deps"
	@echo "  install-dev          Install + dev deps (pytest, ruff, mypy)"
	@echo "  gmgn-keygen          Generate Ed25519 keypair untuk GMGN"
	@echo "  wallet-gen           Generate Solana hot wallet baru"
	@echo "  db-init              Run base Postgres schema migration (001)"
	@echo "  db-migrate-phase10   Run Phase 10 migrations (strategies + price_alerts)"
	@echo "  db-migrate-phase11   Run Phase 11 migrations (trench_low_mcap + position overrides)"
	@echo "  deploy               Run deploy/install.sh untuk VPS production"
	@echo ""
	@echo "Smart Wallet Registry:"
	@echo "  bootstrap-wallets    Discovery dari nol — 200 candidate, ~60 detik"
	@echo "  refresh-wallets      Re-classify existing + add new (run via cron tiap 6 jam)"
	@echo "  list-wallets         Tampilkan semua active smart wallets"
	@echo "  stats-wallets        Quick stats per tier"
	@echo ""
	@echo "Phase 10/11 Strategy & Position Mgmt:"
	@echo "  list-strategies      Show all strategies with enabled flag"
	@echo "  show-pending-alerts  Show dip-buy price alerts waiting for trigger"
	@echo "  show-overrides       Show open positions with active TP/SL/Trail overrides"
	@echo "  pnl-30d              30-day PnL breakdown by exit reason"
	@echo ""
	@echo "Backtest:"
	@echo "  backtest             Full backtest: fetch data + replay + decision gate"
	@echo "  backtest-cached      Replay pakai data cache yang sudah ada (lebih cepat)"
	@echo ""
	@echo "Smoke tests:"
	@echo "  smoke                Phase 1-5 smoke test (11 sources)"
	@echo "  intel-smoke          Phase 7 intel layer smoke test"
	@echo "  phase9-smoke         Phase 9 extended intel smoke test"
	@echo ""
	@echo "Run:"
	@echo "  run                  Run bot (foreground, untuk dev/testing)"
	@echo "  health-check         Curl /health endpoint"
	@echo "  metrics              Curl /metrics endpoint"
	@echo ""
	@echo "Dev:"
	@echo "  test                 Run pytest unit tests (494 tests)"
	@echo "  lint                 Ruff lint check"
	@echo "  format               Ruff format apply"
	@echo "  type-check           Mypy strict check"
	@echo "  ai-cost              Show today's LLM spend"
	@echo "  clean                Remove cache, build, venv"

install:
	python3.11 -m venv venv
	. venv/bin/activate && pip install --upgrade pip && pip install -e .

install-dev:
	python3.11 -m venv venv
	. venv/bin/activate && pip install --upgrade pip && pip install -e ".[dev]"

test:
	. venv/bin/activate && pytest tests/ -v

smoke:
	. venv/bin/activate && python scripts/test_connections.py

lint:
	. venv/bin/activate && ruff check src tests scripts

format:
	. venv/bin/activate && ruff format src tests scripts

type-check:
	. venv/bin/activate && mypy src

gmgn-keygen:
	@mkdir -p secrets
	@if [ -f secrets/gmgn_private.pem ]; then \
		echo "ERROR: secrets/gmgn_private.pem sudah ada. Hapus manual kalau mau replace."; \
		exit 1; \
	fi
	openssl genpkey -algorithm ed25519 -out secrets/gmgn_private.pem
	openssl pkey -in secrets/gmgn_private.pem -pubout -out secrets/gmgn_public.pem
	chmod 600 secrets/gmgn_private.pem secrets/gmgn_public.pem
	@echo ""
	@echo "✓ Keypair generated. Public key (upload ke gmgn.ai/ai):"
	@echo "============================================="
	@cat secrets/gmgn_public.pem
	@echo "============================================="

wallet-gen:
	. venv/bin/activate && python scripts/generate_wallet.py

bootstrap-wallets:
	. venv/bin/activate && python scripts/manage_smart_wallets.py bootstrap --sample 200

refresh-wallets:
	. venv/bin/activate && python scripts/refresh_smart_wallets.py

list-wallets:
	. venv/bin/activate && python scripts/manage_smart_wallets.py list

stats-wallets:
	. venv/bin/activate && python scripts/manage_smart_wallets.py stats

backtest:
	. venv/bin/activate && python scripts/run_backtest.py --sample 50

backtest-cached:
	. venv/bin/activate && python scripts/run_backtest.py --cached

run:
	. venv/bin/activate && python -m src.main

db-init:
	@echo "Applying all SQL migrations in order..."
	@for f in migrations/*.sql; do \
		echo "=== $$f ==="; \
		sudo -u postgres psql -d solana_bot < $$f || { \
			echo "Migration $$f failed"; exit 1; \
		}; \
	done
	@echo "✓ All migrations applied."

db-migrate-phase10:
	@echo "Running Phase 10 migrations (strategies + price_alerts)..."
	sudo -u postgres psql -d solana_bot < migrations/002_strategies.sql
	sudo -u postgres psql -d solana_bot < migrations/003_price_alerts.sql
	@echo "Phase 10 migrations done. 4 strategies seeded: conservative, balanced, aggressive, dip_buy"

db-migrate-phase11:
	@echo "Running Phase 11 migrations (trench_low_mcap + position overrides)..."
	sudo -u postgres psql -d solana_bot < migrations/004_trench_low_mcap.sql
	sudo -u postgres psql -d solana_bot < migrations/005_position_overrides.sql
	@echo "Phase 11 migrations done. 5th strategy 'trench_low_mcap' available."
	@echo "Activate with /strategy trench_low_mcap in Telegram."

list-strategies:
	psql -U bot -d solana_bot -c "SELECT id, name, enabled FROM strategies ORDER BY id;"

show-pending-alerts:
	psql -U bot -d solana_bot -c "SELECT id, mint, symbol, strategy_id, alert_type, target_ath_distance_pct FROM price_alerts WHERE status='pending' ORDER BY detected_at_ms DESC LIMIT 20;"

show-overrides:
	@echo "Open positions with active TP/SL/Trail overrides (Phase 11.1):"
	psql -U bot -d solana_bot -c "SELECT id, token_symbol, tp_override_pct, sl_override_pct, trail_disabled, override_set_by FROM positions WHERE status='OPEN' AND (tp_override_pct IS NOT NULL OR sl_override_pct IS NOT NULL OR trail_disabled = TRUE) ORDER BY override_set_at_ms DESC;"

pnl-30d:
	psql -U bot -d solana_bot -c "SELECT exit_reason, COUNT(*) AS count, ROUND(AVG(realized_pnl_pct)::numeric, 2) AS avg_pct, ROUND(SUM(realized_pnl_sol)::numeric, 6) AS total_sol FROM positions WHERE status='CLOSED' AND exit_timestamp >= NOW() - INTERVAL '30 days' GROUP BY exit_reason ORDER BY count DESC;"

deploy:
	bash deploy/install.sh

nansen-discover:
	. venv/bin/activate && python scripts/nansen_discover.py

intel-smoke:
	. venv/bin/activate && python scripts/intel_smoke.py

phase9-smoke:
	. venv/bin/activate && python scripts/phase9_smoke.py

tuner-run:
	. venv/bin/activate && python scripts/run_weekly_tuner.py

health-check:
	curl -s http://localhost:8080/health | python -m json.tool

metrics:
	curl -s http://localhost:8080/metrics | head -30

ai-cost:
	. venv/bin/activate && python -c "from src.ai.cost_tracker import cost_tracker; print(f'Daily LLM spend: \$\${cost_tracker.daily_spend_usd():.4f}')"

clean:
	rm -rf venv build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
