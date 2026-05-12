"""
Solana wallet manager — load keypair dari file, sign transaction, balance check.

CRITICAL SECURITY:
- File bot-wallet.json berisi PRIVATE KEY (array bytes).
- Permission MUST chmod 600.
- File path dari settings.wallet_path.
- DRY_RUN flag: kalau true, semua sign/submit di-stub jadi no-op (return fake sig).
"""

from __future__ import annotations

import json
from pathlib import Path

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from src.config import settings
from src.infra.logger import get_logger

log = get_logger(__name__)


class WalletManager:
    """Manage Solana hot wallet untuk bot."""

    def __init__(self, wallet_path: Path | None = None) -> None:
        self.wallet_path = wallet_path or settings.wallet_path
        self._keypair: Keypair | None = None

    def load(self) -> Keypair:
        """Load keypair dari file. Cache hasil supaya hemat I/O."""
        if self._keypair is not None:
            return self._keypair

        if not self.wallet_path:
            raise ValueError("WALLET_PATH not configured")

        path = Path(self.wallet_path)
        if not path.exists():
            raise FileNotFoundError(f"Wallet file tidak ada: {path}")

        # Verify permission ketat
        mode = path.stat().st_mode & 0o777
        if mode != 0o600:
            log.warning(
                "wallet_permission_warning",
                path=str(path),
                actual=oct(mode),
                expected="0o600",
                message="Wallet file harus chmod 600",
            )

        try:
            data = json.loads(path.read_text())
            if not isinstance(data, list):
                raise ValueError("Wallet file format invalid (expected JSON array)")
            self._keypair = Keypair.from_bytes(bytes(data))
        except Exception as e:
            log.error("wallet_load_failed", error=str(e))
            raise

        log.info("wallet_loaded", pubkey=str(self._keypair.pubkey()))
        return self._keypair

    @property
    def pubkey(self) -> Pubkey:
        return self.load().pubkey()

    @property
    def address(self) -> str:
        return str(self.pubkey)

    def sign_transaction(self, tx_base64: str) -> str:
        """
        Sign Versioned Transaction (base64-encoded dari Jupiter swap API).

        Returns: signed transaction sebagai base64 string.

        Kalau DRY_RUN, log + return tx tanpa sign (placeholder, tidak akan submit).
        """
        if settings.dry_run:
            log.info("wallet_dry_run_skip_sign", tx_size=len(tx_base64))
            return tx_base64  # placeholder, tidak akan benar-benar diproses

        keypair = self.load()
        import base64
        from solders.transaction import VersionedTransaction

        # Decode + sign + re-encode
        tx_bytes = base64.b64decode(tx_base64)
        tx = VersionedTransaction.from_bytes(tx_bytes)
        signed = VersionedTransaction(tx.message, [keypair])
        signed_bytes = bytes(signed)
        return base64.b64encode(signed_bytes).decode("ascii")

    async def get_balance_sol(self, rpc_client) -> float:  # type: ignore[no-untyped-def]
        """Convenience: get SOL balance via Helius RPC client."""
        lamports = await rpc_client.get_balance(self.address)
        return lamports / 1_000_000_000


# Module-level singleton — inisialisasi on first use
_wallet: WalletManager | None = None


def get_wallet() -> WalletManager:
    global _wallet
    if _wallet is None:
        _wallet = WalletManager()
    return _wallet
