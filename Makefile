.PHONY: help install install-dev test lint format type-check smoke clean run-dev gmgn-keygen wallet-gen bootstrap-wallets refresh-wallets list-wallets stats-wallets backtest backtest-cached run db-init deploy

help:
	@echo "Solana Sniper Bot — Make targets:"
	@echo ""
	@echo "Setup:"
	@echo "  install            Install runtime deps"
	@echo "  install-dev        Install + dev deps (pytest, ruff, mypy)"
	@echo "  gmgn-keygen        Generate Ed25519 keypair untuk GMGN"
	@echo "  wallet-gen         Generate Solana hot wallet baru"
	@echo "  db-init            Run Postgres schema migration"
	@echo "  deploy             Run deploy/install.sh untuk VPS production"
	@echo ""
	@echo "Smart Wallet Registry:"
	@echo "  bootstrap-wallets  Discovery dari nol — 200 candidate, ~60 detik"
	@echo "  refresh-wallets    Re-classify existing + add new (run via cron tiap 6 jam)"
	@echo "  list-wallets       Tampilkan semua active smart wallets"
	@echo "  stats-wallets      Quick stats per tier"
	@echo ""
	@echo "Backtest:"
	@echo "  backtest           Full backtest: fetch data + replay + decision gate"
	@echo "  backtest-cached    Replay pakai data cache yang sudah ada (lebih cepat)"
	@echo ""
	@echo "Run:"
	@echo "  run                Run bot (foreground, untuk dev/testing)"
	@echo ""
	@echo "Dev:"
	@echo "  test               Run pytest unit tests (72 tests)"
	@echo "  smoke              Smoke test live API (Helius, GMGN, GeckoTerminal)"
	@echo "  lint               Ruff lint check"
	@echo "  format             Ruff format apply"
	@echo "  type-check         Mypy strict check"
	@echo "  clean              Remove cache, build, venv"

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
	psql -U bot -d solana_bot -f migrations/001_initial.sql

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
