"""
Generate Solana hot wallet baru untuk bot.

Output:
- secrets/bot-wallet.json (Solana keypair, format byte array)
- Print public key (address) ke stdout

Usage:
    python scripts/generate_wallet.py

Setelah jalan:
1. Catat address di output untuk top-up dari CEX
2. Pastikan secrets/bot-wallet.json chmod 600
3. JANGAN commit, JANGAN share
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from solders.keypair import Keypair


def main() -> int:
    output_path = Path("secrets/bot-wallet.json")

    if output_path.exists():
        print(f"ERROR: {output_path} sudah ada.")
        print("Hapus manual kalau mau replace (BACKUP DULU kalau ada saldo!).")
        return 1

    output_path.parent.mkdir(parents=True, exist_ok=True)

    keypair = Keypair()
    pubkey = str(keypair.pubkey())
    secret_bytes = list(bytes(keypair))

    output_path.write_text(json.dumps(secret_bytes))
    output_path.chmod(0o600)

    print("=" * 60)
    print("SOLANA HOT WALLET GENERATED")
    print("=" * 60)
    print(f"Public address (untuk top-up): {pubkey}")
    print(f"Private key file: {output_path.absolute()}")
    print(f"Permission: {oct(output_path.stat().st_mode)[-3:]}")
    print()
    print("LANGKAH BERIKUTNYA:")
    print(f"1. Catat address di password manager: {pubkey}")
    print("2. Top-up 0.36 SOL dari CEX (Indodax / Tokocrypto / Binance)")
    print(f"   Solscan: https://solscan.io/account/{pubkey}")
    print("3. Tambahkan ke secrets/.env:")
    print(f"   WALLET_PUBLIC_KEY={pubkey}")
    print("   WALLET_PATH=secrets/bot-wallet.json")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
