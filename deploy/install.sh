#!/bin/bash
# Deploy script untuk VPS Tencent SG.
# Jalankan SETELAH Phase 0 setup (lihat docs Phase 0).
#
# Usage: bash deploy/install.sh

set -e

REPO_DIR="${REPO_DIR:-/home/bot/solana-bot}"
SERVICE_FILE="$REPO_DIR/deploy/solana-bot.service"

echo "=== Solana Sniper Bot — Deploy Setup ==="

# 1. Verify dependencies
for cmd in python3.11 git psql redis-cli systemctl; do
    if ! command -v "$cmd" &> /dev/null; then
        echo "ERROR: $cmd not found. Install dulu (lihat Phase 0 doc step 6.G)"
        exit 1
    fi
done

# 2. Verify project location
if [ ! -d "$REPO_DIR" ]; then
    echo "ERROR: Project tidak di $REPO_DIR. Set REPO_DIR env atau clone ke sana."
    exit 1
fi

cd "$REPO_DIR"

# Install Node.js + npm if not present (for gmgn-cli and nansen-cli)
if ! command -v node &> /dev/null; then
    echo "Installing Node.js 20..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
    sudo apt-get install -y nodejs
fi

# Install gmgn-cli and nansen-cli globally
if ! command -v gmgn-cli &> /dev/null; then
    echo "Installing gmgn-cli..."
    sudo npm install -g gmgn-cli
fi

if ! command -v nansen-cli &> /dev/null; then
    echo "Installing nansen-cli..."
    sudo npm install -g nansen-cli
fi

# Verify CLI tools
echo "Verifying CLI tools..."
gmgn-cli --version || echo "WARNING: gmgn-cli failed verification"
nansen-cli --version || echo "WARNING: nansen-cli failed verification"

# 3. Setup venv + install deps
if [ ! -d "venv" ]; then
    echo "Creating venv..."
    python3.11 -m venv venv
fi

source venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -e .
echo "✓ Dependencies installed"

# 4. Verify .env exists
if [ ! -f "secrets/.env" ]; then
    echo "ERROR: secrets/.env tidak ada. Copy dari .env.example dan isi credential."
    exit 1
fi

# Verify chmod 600
PERM=$(stat -c %a secrets/.env)
if [ "$PERM" != "600" ]; then
    echo "Fixing permission secrets/.env to 600..."
    chmod 600 secrets/.env
fi

# 5. Initialize Postgres schema
echo "Running DB migration..."
psql -U bot -d solana_bot -f migrations/001_initial.sql || {
    echo "WARNING: migration failed (mungkin sudah pernah jalan, ok)"
}

# 6. Smoke test
echo "Running smoke test..."
python scripts/test_connections.py || {
    echo "ERROR: smoke test gagal. Fix dulu sebelum lanjut."
    exit 1
}

# 7. Bootstrap smart wallets (kalau belum)
if [ ! -f "data/smart_wallets.json" ]; then
    echo "Bootstrapping smart wallet registry..."
    python scripts/manage_smart_wallets.py bootstrap --sample 200 || {
        echo "WARNING: bootstrap failed. Bisa di-retry manual nanti."
    }
fi

# 8. Install systemd service
if [ -f "$SERVICE_FILE" ]; then
    echo "Installing systemd service..."
    sudo cp "$SERVICE_FILE" /etc/systemd/system/solana-bot.service
    sudo systemctl daemon-reload
    sudo systemctl enable solana-bot
    echo "✓ Systemd service installed"
fi

# 9. Setup cron untuk smart wallet refresh
CRON_LINE="0 */6 * * * cd $REPO_DIR && $REPO_DIR/venv/bin/python scripts/refresh_smart_wallets.py >> $REPO_DIR/logs/refresh.log 2>&1"
if ! crontab -l 2>/dev/null | grep -q "refresh_smart_wallets.py"; then
    (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
    echo "✓ Cron job added: refresh_smart_wallets every 6h"
fi

echo ""
echo "=== DEPLOY DONE ==="
echo ""
echo "Next steps:"
echo "  1. Verify .env DRY_RUN=true"
echo "  2. Start bot: sudo systemctl start solana-bot"
echo "  3. Check logs: sudo journalctl -u solana-bot -f"
echo "  4. Telegram /status untuk verify bot running"
echo "  5. Monitor 24-48 jam dengan DRY_RUN, lihat alerts"
echo "  6. Setelah yakin, edit .env DRY_RUN=false untuk live trading"
echo ""
