"""
Helius Sender — submit signed transaction dengan parallel routing ke Jito + Helius.

GRATIS di Helius free tier (cuma butuh tip 0.001 SOL minimum). Memberikan:
- MEV protection via Jito bundle
- Parallel route ke 7+ regional endpoints
- Faster inclusion than standard sendTransaction

Docs: https://www.helius.dev/docs/sender
Endpoint pattern: POST https://sender.helius-rpc.com/fast?api-key=YOUR_KEY
"""

from __future__ import annotations

from typing import Any

from src.clients.base import BaseHTTPClient, HTTPError
from src.config import settings
from src.infra.logger import get_logger

log = get_logger(__name__)


class HeliusSenderClient:
    """
    Submit transaction via Helius Sender. No credits billed (cuma SOL tip).

    Tip Solana wallet (Jito tip account): minimum 0.001 SOL = 1_000_000 lamports.
    Tip DI-INCLUDE dalam transaction (sebagai instruction transfer ke Jito tip account).
    """

    # Jito tip accounts (rotate untuk distribute load) — official list
    JITO_TIP_ACCOUNTS = [
        "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
        "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
        "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
        "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
        "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
        "ADuUkR4vqLUMWXxW9gh6D6L8pivKeVBBjQxAk6jbpGS3",
        "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
        "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
    ]

    def __init__(self) -> None:
        self.api_key = settings.helius_api_key
        if not self.api_key:
            raise ValueError("HELIUS_API_KEY not set")

        self._http = BaseHTTPClient(
            base_url=f"https://sender.helius-rpc.com",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=30.0,
            max_retries=2,
        )

    async def close(self) -> None:
        await self._http.close()

    async def __aenter__(self) -> "HeliusSenderClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def send_transaction(
        self,
        signed_tx_base64: str,
        skip_preflight: bool = True,
        max_retries: int = 0,  # Sender re-broadcasts internally
    ) -> str:
        """
        Submit signed transaction via Sender (parallel Jito + Helius routing).

        Args:
            signed_tx_base64: Base64-encoded SIGNED transaction (sudah include tip)
            skip_preflight: True direkomendasi untuk speed (sender tetap validate)
            max_retries: 0 = trust sender's internal rebroadcast

        Returns: transaction signature (base58 string)

        DRY_RUN: kalau true, log + return fake signature, NOT submitted to chain.
        """
        if settings.dry_run:
            fake_sig = "DRY_RUN_" + signed_tx_base64[:32]
            log.info("sender_dry_run", fake_sig=fake_sig)
            return fake_sig

        path = f"/fast?api-key={self.api_key}"
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                signed_tx_base64,
                {
                    "encoding": "base64",
                    "skipPreflight": skip_preflight,
                    "maxRetries": max_retries,
                },
            ],
        }
        response = await self._http.post(path, json=body)

        if "error" in response:
            err = response["error"]
            log.error("sender_error", error=err)
            raise HTTPError(
                f"Sender error: {err.get('message')}",
                status=err.get("code"),
                body=str(err),
            )

        signature = response.get("result", "")
        log.info("sender_submitted", signature=signature)
        return signature

    @classmethod
    def get_random_tip_account(cls) -> str:
        """Return Jito tip account (rotate untuk load balance)."""
        import random

        return random.choice(cls.JITO_TIP_ACCOUNTS)

    @classmethod
    def build_tip_instruction(cls, tip_lamports: int, payer_pubkey: str) -> dict[str, Any]:
        """
        Build Solana SystemProgram.Transfer instruction untuk tip ke Jito.

        NOTE: Untuk Jupiter swap, tip bisa di-INCLUDE dalam transaction via
        `prioritizationFeeLamports.jitoTipLamports` di /v6/swap API. Lebih simpel
        daripada bangun instruction manual.

        Returns dict format yang bisa di-merge ke transaction message.
        """
        return {
            "programId": "11111111111111111111111111111111",
            "accounts": [
                {"pubkey": payer_pubkey, "isSigner": True, "isWritable": True},
                {"pubkey": cls.get_random_tip_account(), "isSigner": False, "isWritable": True},
            ],
            "data": {
                "instruction": "Transfer",
                "lamports": tip_lamports,
            },
        }
