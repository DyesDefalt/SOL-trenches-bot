"""
Execution Layer — buy/sell trade lewat Jupiter + Helius Sender (default)
                  atau GMGN swap CLI (provider="gmgn").

Flow buy (Jupiter):
1. Get quote dari Jupiter (SOL → token)
2. Build swap transaction dengan priority fee + Jito tip
3. Sign transaction dengan wallet manager
4. Submit via Helius Sender (parallel Jito + Helius routing, anti-MEV)
5. Wait konfirmasi via getSignatureStatus polling
6. Insert ke DB

Flow buy (GMGN):
1. Build condition_orders dari settings TP/SL config
2. Call gmgn-cli swap subprocess
3. Parse stdout → TradeResult

DRY_RUN: kalau true, semua step di-stub. Quote tetap di-fetch (validasi liquidity)
tapi sign + submit di-skip. Insert ke DB dengan dry_run=true flag.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

from src.clients.helius import HeliusRPCClient
from src.clients.helius_sender import HeliusSenderClient
from src.clients.jupiter import JupiterClient, SOL_MINT
from src.config import settings
from src.infra.logger import get_logger
from src.infra.wallet import WalletManager

log = get_logger(__name__)


@dataclass
class TradeResult:
    """Hasil execute trade."""

    success: bool
    signature: str = ""
    in_amount: int = 0
    out_amount: int = 0
    price_impact_pct: float = 0.0
    error: str = ""
    dry_run: bool = False


class ExecutionLayer:
    """Multi-provider execution coordinator.

    Dispatches buy/sell to Jupiter+Helius (default) or GMGN swap CLI
    based on settings.execution_provider.
    """

    def __init__(
        self,
        wallet: WalletManager,
        jupiter: JupiterClient,
        sender: HeliusSenderClient,
        rpc: HeliusRPCClient,
        gmgn_swap: object | None = None,  # GMGNSwapClient | None — avoid circular at top level
    ) -> None:
        self.wallet = wallet
        self.jupiter = jupiter
        self.sender = sender
        self.rpc = rpc
        self.gmgn_swap = gmgn_swap

    # ------------------------------------------------------------------
    # Public API — dispatch by provider
    # ------------------------------------------------------------------

    async def buy_token(
        self,
        token_address: str,
        sol_amount: float,
        slippage_bps: int | None = None,
        priority_fee_microlamports: int | None = None,
    ) -> TradeResult:
        """Route buy to provider based on settings.execution_provider."""
        if settings.execution_provider == "gmgn" and self.gmgn_swap is not None:
            return await self._buy_via_gmgn(token_address, sol_amount, slippage_bps)
        return await self._buy_via_jupiter(
            token_address, sol_amount, slippage_bps, priority_fee_microlamports
        )

    async def sell_token(
        self,
        token_address: str,
        token_amount: int,
        slippage_bps: int | None = None,
        priority_fee_microlamports: int | None = None,
    ) -> TradeResult:
        """Route sell to provider based on settings.execution_provider."""
        if settings.execution_provider == "gmgn" and self.gmgn_swap is not None:
            return await self._sell_via_gmgn(token_address, token_amount, slippage_bps)
        return await self._sell_via_jupiter(
            token_address, token_amount, slippage_bps, priority_fee_microlamports
        )

    # ------------------------------------------------------------------
    # Jupiter path (original logic)
    # ------------------------------------------------------------------

    async def _buy_via_jupiter(
        self,
        token_address: str,
        sol_amount: float,
        slippage_bps: int | None,
        priority_fee_microlamports: int | None,
    ) -> TradeResult:
        """
        Buy token dengan SOL via Jupiter + Helius Sender.

        Args:
            token_address: token mint address
            sol_amount: jumlah SOL untuk swap (e.g., 0.025)
            slippage_bps: max slippage (default dari settings)
            priority_fee_microlamports: priority fee compute unit

        Returns: TradeResult dengan success flag + signature
        """
        lamports = int(sol_amount * 1_000_000_000)
        log.info("buy_start", token=token_address, sol=sol_amount, lamports=lamports)

        try:
            quote, tx_base64 = await self.jupiter.quote_and_swap_tx(
                input_mint=SOL_MINT,
                output_mint=token_address,
                amount=lamports,
                user_public_key=self.wallet.address,
                slippage_bps=slippage_bps,
                priority_fee_microlamports=priority_fee_microlamports,
            )
        except Exception as e:
            log.error("buy_quote_failed", token=token_address, error=str(e))
            return TradeResult(success=False, error=f"quote_failed: {e}")

        summary = JupiterClient.parse_quote_summary(quote)
        log.info("buy_quote", token=token_address, **summary)

        # Sanity check price impact
        if summary["price_impact_pct"] > 25:
            log.warning(
                "buy_skip_high_impact",
                token=token_address,
                impact=summary["price_impact_pct"],
            )
            return TradeResult(
                success=False,
                error=f"price_impact_too_high ({summary['price_impact_pct']:.1f}%)",
            )

        # Sign + submit
        try:
            signed_tx = self.wallet.sign_transaction(tx_base64)
        except Exception as e:
            log.error("buy_sign_failed", error=str(e))
            return TradeResult(success=False, error=f"sign_failed: {e}")

        try:
            signature = await self.sender.send_transaction(signed_tx)
        except Exception as e:
            log.error("buy_submit_failed", error=str(e))
            return TradeResult(success=False, error=f"submit_failed: {e}")

        log.info("buy_submitted", token=token_address, signature=signature)

        result = TradeResult(
            success=True,
            signature=signature,
            in_amount=int(quote.get("inAmount", 0)),
            out_amount=int(quote.get("outAmount", 0)),
            price_impact_pct=summary["price_impact_pct"],
            dry_run=settings.dry_run,
        )

        # Wait konfirmasi (skip kalau dry run)
        if not settings.dry_run:
            confirmed = await self._wait_confirmation(signature)
            if not confirmed:
                log.warning("buy_confirmation_timeout", signature=signature)
                result.success = False
                result.error = "confirmation_timeout"

        return result

    async def _sell_via_jupiter(
        self,
        token_address: str,
        token_amount: int,
        slippage_bps: int | None,
        priority_fee_microlamports: int | None,
    ) -> TradeResult:
        """
        Sell token jadi SOL via Jupiter + Helius Sender.

        Args:
            token_address: token mint address
            token_amount: jumlah token (atomic units, sudah di-multiply oleh decimals)
        """
        log.info("sell_start", token=token_address, amount=token_amount)

        try:
            quote, tx_base64 = await self.jupiter.quote_and_swap_tx(
                input_mint=token_address,
                output_mint=SOL_MINT,
                amount=token_amount,
                user_public_key=self.wallet.address,
                slippage_bps=slippage_bps,
                priority_fee_microlamports=priority_fee_microlamports,
            )
        except Exception as e:
            log.error("sell_quote_failed", error=str(e))
            return TradeResult(success=False, error=f"quote_failed: {e}")

        summary = JupiterClient.parse_quote_summary(quote)
        log.info("sell_quote", token=token_address, **summary)

        try:
            signed_tx = self.wallet.sign_transaction(tx_base64)
            signature = await self.sender.send_transaction(signed_tx)
        except Exception as e:
            log.error("sell_failed", error=str(e))
            return TradeResult(success=False, error=str(e))

        log.info("sell_submitted", token=token_address, signature=signature)

        result = TradeResult(
            success=True,
            signature=signature,
            in_amount=int(quote.get("inAmount", 0)),
            out_amount=int(quote.get("outAmount", 0)),
            price_impact_pct=summary["price_impact_pct"],
            dry_run=settings.dry_run,
        )

        if not settings.dry_run:
            confirmed = await self._wait_confirmation(signature)
            if not confirmed:
                result.success = False
                result.error = "confirmation_timeout"

        return result

    # ------------------------------------------------------------------
    # GMGN path (Phase 7g)
    # ------------------------------------------------------------------

    async def _buy_via_gmgn(
        self,
        token_address: str,
        sol_amount: float,
        slippage_bps: int | None,
    ) -> TradeResult:
        """Buy via GMGN swap CLI with auto-built TP/SL condition orders."""
        # Convert slippage_bps → fraction (gmgn-cli expects 0.15 not 1500)
        slippage_frac = (slippage_bps if slippage_bps is not None else settings.slippage_bps) / 10_000.0

        # Build condition orders from settings TP/SL config
        condition_orders = self._build_condition_orders()

        resp = await self.gmgn_swap.buy_token(  # type: ignore[union-attr]
            token_address=token_address,
            sol_amount=sol_amount,
            slippage=slippage_frac,
            condition_orders=condition_orders if condition_orders else None,
        )

        if not resp.get("success"):
            err = resp.get("error", "gmgn_buy_failed")
            log.error("gmgn_buy_failed", token=token_address, error=err)
            return TradeResult(success=False, error=err)

        lamports = int(sol_amount * 1_000_000_000)
        return TradeResult(
            success=True,
            signature=resp.get("signature", ""),
            in_amount=lamports,
            out_amount=resp.get("out_amount", 0),
            price_impact_pct=float(resp.get("price_impact_pct", 0.0)),
            dry_run=bool(resp.get("dry_run", settings.dry_run)),
        )

    async def _sell_via_gmgn(
        self,
        token_address: str,
        token_amount: int,
        slippage_bps: int | None,
    ) -> TradeResult:
        """Sell via GMGN swap CLI."""
        slippage_frac = (slippage_bps if slippage_bps is not None else settings.slippage_bps) / 10_000.0

        resp = await self.gmgn_swap.sell_token(  # type: ignore[union-attr]
            token_address=token_address,
            token_amount_smallest=token_amount,
            slippage=slippage_frac,
        )

        if not resp.get("success"):
            err = resp.get("error", "gmgn_sell_failed")
            log.error("gmgn_sell_failed", token=token_address, error=err)
            return TradeResult(success=False, error=err)

        return TradeResult(
            success=True,
            signature=resp.get("signature", ""),
            in_amount=token_amount,
            out_amount=resp.get("out_amount", 0),
            price_impact_pct=float(resp.get("price_impact_pct", 0.0)),
            dry_run=bool(resp.get("dry_run", settings.dry_run)),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_condition_orders(self) -> list[dict[str, str]]:
        """Build GMGN condition order list from TP/SL settings."""
        orders: list[dict[str, str]] = []

        # TP1
        if settings.tp1_gain_pct and settings.tp1_sell_pct:
            orders.append({
                "order_type": "profit_stop",
                "side": "sell",
                "price_scale": str(int(settings.tp1_gain_pct)),
                "sell_ratio": str(int(settings.tp1_sell_pct)),
            })

        # TP2
        if settings.tp2_gain_pct and settings.tp2_sell_pct:
            orders.append({
                "order_type": "profit_stop",
                "side": "sell",
                "price_scale": str(int(settings.tp2_gain_pct)),
                "sell_ratio": str(int(settings.tp2_sell_pct)),
            })

        # TP3 with trailing
        if settings.tp3_gain_pct and settings.tp3_sell_pct:
            orders.append({
                "order_type": "profit_stop_trace",
                "side": "sell",
                "price_scale": str(int(settings.tp3_gain_pct)),
                "drawdown_rate": str(int(settings.trailing_stop_pct)),
                "sell_ratio": str(int(settings.tp3_sell_pct)),
            })

        # Hard SL (hard_sl_pct is negative, e.g. -45)
        if settings.hard_sl_pct:
            sl_positive = abs(int(settings.hard_sl_pct))
            orders.append({
                "order_type": "loss_stop",
                "side": "sell",
                "price_scale": str(sl_positive),
                "sell_ratio": "100",
            })

        return orders

    async def _wait_confirmation(
        self,
        signature: str,
        max_wait_seconds: int = 60,
        poll_interval: float = 2.0,
    ) -> bool:
        """Poll getSignatureStatus sampai confirmed atau timeout."""
        elapsed = 0.0
        while elapsed < max_wait_seconds:
            try:
                statuses = await self.rpc.call("getSignatureStatuses", [[signature]])
                if statuses and statuses.get("value") and statuses["value"][0]:
                    status = statuses["value"][0]
                    if status.get("confirmationStatus") in ("confirmed", "finalized"):
                        return True
                    if status.get("err"):
                        log.error("tx_failed_onchain", signature=signature, err=status["err"])
                        return False
            except Exception as e:
                log.warning("confirmation_poll_error", error=str(e))

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        return False
