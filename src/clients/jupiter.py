"""
Jupiter Swap Aggregator client.

Jupiter cari best route + price across all Solana DEXes (Raydium, Orca, Meteora,
Pump.fun, dll). Free tier: cukup untuk MVP.

Flow trading:
1. `get_quote(input_mint, output_mint, amount, slippage_bps)` → quote with route
2. `get_swap_transaction(quote, user_pubkey)` → unsigned tx (base64)
3. Sign + submit via Helius Sender (anti-MEV) atau standard RPC

Docs: https://station.jup.ag/docs/swap-api/swap-api
"""

from __future__ import annotations

import base64
from typing import Any, Literal

from src.clients.base import BaseHTTPClient
from src.config import settings
from src.infra.logger import get_logger

log = get_logger(__name__)

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

SwapMode = Literal["ExactIn", "ExactOut"]


class JupiterClient:
    """
    Jupiter v6 API client (Free tier).

    Production note: Free tier rate-limited. Untuk volume tinggi, upgrade ke Pro
    di https://portal.jup.ag — tapi MVP cukup free.
    """

    BASE_URL = "https://quote-api.jup.ag/v6"

    def __init__(self) -> None:
        self._http = BaseHTTPClient(
            base_url=self.BASE_URL,
            headers={
                "Accept": "application/json",
                "User-Agent": "solana-sniper-bot/0.1",
            },
            timeout=15.0,
            max_retries=2,
        )

    async def close(self) -> None:
        await self._http.close()

    async def __aenter__(self) -> "JupiterClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,  # in smallest unit (lamports for SOL, atomic units for SPL)
        slippage_bps: int | None = None,
        swap_mode: SwapMode = "ExactIn",
        only_direct_routes: bool = False,
        as_legacy_transaction: bool = False,
    ) -> dict[str, Any]:
        """
        Get swap quote.

        Args:
            input_mint: token mint kamu jual (e.g., SOL untuk buy)
            output_mint: token mint kamu beli
            amount: jumlah dalam unit terkecil (lamports / atomic)
            slippage_bps: max slippage basis points (1500 = 15%)
            swap_mode: ExactIn (kasih X dapat ?) or ExactOut (kasih ? dapat X)

        Returns dict dengan: outAmount, priceImpactPct, routePlan, dll.
        """
        params: dict[str, Any] = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": amount,
            "slippageBps": slippage_bps if slippage_bps is not None else settings.slippage_bps,
            "swapMode": swap_mode,
            "onlyDirectRoutes": str(only_direct_routes).lower(),
            "asLegacyTransaction": str(as_legacy_transaction).lower(),
        }
        return await self._http.get("/quote", params=params)

    async def get_swap_transaction(
        self,
        quote: dict[str, Any],
        user_public_key: str,
        priority_fee_lamports: int | None = None,
        compute_unit_price_micro_lamports: int | None = None,
        wrap_and_unwrap_sol: bool = True,
        as_legacy_transaction: bool = False,
    ) -> dict[str, Any]:
        """
        Build swap transaction berdasarkan quote.

        Returns: {
            "swapTransaction": "<base64-encoded versioned transaction>",
            "lastValidBlockHeight": int,
            ...
        }

        Transaction masih perlu di-sign + submit ke RPC / Helius Sender.
        """
        body: dict[str, Any] = {
            "quoteResponse": quote,
            "userPublicKey": user_public_key,
            "wrapAndUnwrapSol": wrap_and_unwrap_sol,
            "asLegacyTransaction": as_legacy_transaction,
        }

        # Priority fee handling
        if priority_fee_lamports is not None:
            body["prioritizationFeeLamports"] = priority_fee_lamports
        elif compute_unit_price_micro_lamports is not None:
            body["computeUnitPriceMicroLamports"] = compute_unit_price_micro_lamports
        else:
            # Default: pakai dari settings
            body["computeUnitPriceMicroLamports"] = settings.priority_fee_microlamports

        return await self._http.post("/swap", json=body)

    async def quote_and_swap_tx(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        user_public_key: str,
        slippage_bps: int | None = None,
        priority_fee_microlamports: int | None = None,
    ) -> tuple[dict[str, Any], str]:
        """
        Convenience: quote + build swap tx dalam 1 call.

        Returns: (quote, base64_unsigned_transaction)
        """
        quote = await self.get_quote(
            input_mint=input_mint,
            output_mint=output_mint,
            amount=amount,
            slippage_bps=slippage_bps,
        )
        if "error" in quote or not quote.get("outAmount"):
            log.error("jupiter_quote_failed", quote=quote)
            raise ValueError(f"Quote failed: {quote}")

        swap = await self.get_swap_transaction(
            quote=quote,
            user_public_key=user_public_key,
            compute_unit_price_micro_lamports=priority_fee_microlamports,
        )

        tx_base64 = swap.get("swapTransaction", "")
        if not tx_base64:
            raise ValueError(f"Swap tx build failed: {swap}")

        return quote, tx_base64

    @staticmethod
    def parse_quote_summary(quote: dict[str, Any]) -> dict[str, Any]:
        """Extract human-readable summary dari quote untuk logging."""
        return {
            "in_amount": quote.get("inAmount"),
            "out_amount": quote.get("outAmount"),
            "out_amount_with_slippage": quote.get("otherAmountThreshold"),
            "price_impact_pct": float(quote.get("priceImpactPct", 0)) * 100,
            "route_count": len(quote.get("routePlan", [])),
            "slippage_bps": quote.get("slippageBps"),
        }
