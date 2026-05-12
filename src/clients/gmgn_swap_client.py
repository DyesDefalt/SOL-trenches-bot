"""
GMGN Swap Client — wraps gmgn-cli via subprocess.

Provides native anti-MEV via Jito + GMGN routing, condition orders
(TP staircase, SL, trailing) in a single tx, and slippage auto-adjust.

Trade-off: ~50–100ms subprocess overhead vs Jupiter direct HTTP.

DRY_RUN: kalau settings.dry_run=True, log + return fake response without
executing subprocess.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any

from src.config import settings
from src.infra.logger import get_logger

log = get_logger(__name__)


class GMGNSwapClient:
    """GMGN swap wrapper via gmgn-cli subprocess.

    Native support for:
    - Anti-MEV via Jito + GMGN routing (built-in)
    - Condition orders (TP staircase, SL, trailing) in single tx
    - Slippage auto-adjust

    Trade-off: ~50-100ms subprocess overhead vs Jupiter direct HTTP.
    """

    SOL_MINT = "So11111111111111111111111111111111111111112"

    SUBPROCESS_TIMEOUT = 30  # seconds

    def __init__(self, wallet_address: str) -> None:
        self.wallet_address = wallet_address

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def buy_token(
        self,
        token_address: str,
        sol_amount: float,
        slippage: float = 0.15,  # 15% memecoin-friendly
        priority_fee_sol: float = 0.001,
        tip_fee_sol: float = 0.001,
        condition_orders: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Execute buy via gmgn-cli. Returns dict with status, signature, etc.

        condition_orders example:
        [
            {"order_type": "profit_stop", "side": "sell", "price_scale": "80", "sell_ratio": "30"},
            {"order_type": "profit_stop", "side": "sell", "price_scale": "150", "sell_ratio": "30"},
            {"order_type": "profit_stop_trace", "side": "sell", "price_scale": "300",
             "drawdown_rate": "30", "sell_ratio": "40"},
            {"order_type": "loss_stop", "side": "sell", "price_scale": "45", "sell_ratio": "100"},
        ]
        """
        lamports = int(sol_amount * 1_000_000_000)
        log.info(
            "gmgn_buy_start",
            token=token_address,
            sol=sol_amount,
            lamports=lamports,
            slippage=slippage,
            dry_run=settings.dry_run,
        )

        if settings.dry_run:
            return self._fake_response("buy", token_address)

        cmd = self._build_buy_cmd(
            token_address=token_address,
            lamports=lamports,
            slippage=slippage,
            priority_fee_sol=priority_fee_sol,
            tip_fee_sol=tip_fee_sol,
            condition_orders=condition_orders,
        )
        return await self._run_cmd(cmd, context="buy")

    async def sell_token(
        self,
        token_address: str,
        token_amount_smallest: int,
        slippage: float = 0.15,
    ) -> dict[str, Any]:
        """Execute sell via gmgn-cli.

        Args:
            token_address: token mint address
            token_amount_smallest: amount in atomic units (already multiplied by decimals)
            slippage: max slippage fraction (0.15 = 15%)
        """
        log.info(
            "gmgn_sell_start",
            token=token_address,
            amount=token_amount_smallest,
            slippage=slippage,
            dry_run=settings.dry_run,
        )

        if settings.dry_run:
            return self._fake_response("sell", token_address)

        cmd = self._build_sell_cmd(
            token_address=token_address,
            token_amount_smallest=token_amount_smallest,
            slippage=slippage,
        )
        return await self._run_cmd(cmd, context="sell")

    async def get_order_status(self, order_id: str) -> dict[str, Any]:
        """Query condition order status via gmgn-cli."""
        log.info("gmgn_order_status", order_id=order_id, dry_run=settings.dry_run)

        if settings.dry_run:
            return {"order_id": order_id, "status": "DRY_RUN", "dry_run": True}

        cmd = ["gmgn-cli", "order-status", "--chain", "sol", "--order-id", order_id]
        return await self._run_cmd(cmd, context="order_status")

    async def cancel_strategy_order(self, order_id: str) -> dict[str, Any]:
        """Cancel a condition/strategy order via gmgn-cli."""
        log.info("gmgn_cancel_order", order_id=order_id, dry_run=settings.dry_run)

        if settings.dry_run:
            return {"order_id": order_id, "cancelled": True, "dry_run": True}

        cmd = [
            "gmgn-cli", "cancel-order",
            "--chain", "sol",
            "--order-id", order_id,
            "--from", self.wallet_address,
        ]
        return await self._run_cmd(cmd, context="cancel_order")

    # ------------------------------------------------------------------
    # Command builders
    # ------------------------------------------------------------------

    def _build_buy_cmd(
        self,
        token_address: str,
        lamports: int,
        slippage: float,
        priority_fee_sol: float,
        tip_fee_sol: float,
        condition_orders: list[dict[str, Any]] | None,
    ) -> list[str]:
        cmd = [
            "gmgn-cli", "swap",
            "--chain", "sol",
            "--from", self.wallet_address,
            "--input-token", self.SOL_MINT,
            "--output-token", token_address,
            "--amount", str(lamports),
            "--slippage", str(slippage),
            "--priority-fee", str(priority_fee_sol),
            "--tip-fee", str(tip_fee_sol),
            "--anti-mev",
        ]
        if condition_orders:
            cmd += ["--condition-orders", json.dumps(condition_orders)]
        return cmd

    def _build_sell_cmd(
        self,
        token_address: str,
        token_amount_smallest: int,
        slippage: float,
    ) -> list[str]:
        return [
            "gmgn-cli", "swap",
            "--chain", "sol",
            "--from", self.wallet_address,
            "--input-token", token_address,
            "--output-token", self.SOL_MINT,
            "--amount", str(token_amount_smallest),
            "--slippage", str(slippage),
            "--anti-mev",
        ]

    # ------------------------------------------------------------------
    # Subprocess executor
    # ------------------------------------------------------------------

    async def _run_cmd(self, cmd: list[str], context: str) -> dict[str, Any]:
        """Run gmgn-cli subprocess, parse JSON stdout, handle errors."""
        log.debug("gmgn_cmd", context=context, cmd=cmd)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self.SUBPROCESS_TIMEOUT,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                log.error("gmgn_cmd_timeout", context=context, timeout=self.SUBPROCESS_TIMEOUT)
                return {"success": False, "error": "subprocess_timeout"}

        except FileNotFoundError:
            log.error("gmgn_cli_not_found", cmd=cmd[0])
            return {"success": False, "error": "gmgn_cli_not_found"}
        except Exception as e:
            log.error("gmgn_cmd_exec_failed", context=context, error=str(e))
            return {"success": False, "error": str(e)}

        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            log.error(
                "gmgn_cmd_nonzero",
                context=context,
                returncode=proc.returncode,
                stderr=stderr[:500],
            )
            return {
                "success": False,
                "error": f"exit_code_{proc.returncode}",
                "stderr": stderr[:500],
            }

        if not stdout:
            log.error("gmgn_cmd_empty_stdout", context=context, stderr=stderr[:200])
            return {"success": False, "error": "empty_stdout"}

        try:
            data: dict[str, Any] = json.loads(stdout)
        except json.JSONDecodeError as e:
            log.error("gmgn_cmd_parse_failed", context=context, stdout=stdout[:200], error=str(e))
            return {"success": False, "error": f"json_parse_failed: {e}", "raw": stdout[:200]}

        log.info("gmgn_cmd_ok", context=context, order_id=data.get("order_id"))
        return data

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _fake_response(self, direction: str, token_address: str) -> dict[str, Any]:
        """DRY_RUN stub response — matches shape of real gmgn-cli output."""
        order_id = f"DRY_RUN_{secrets.token_hex(8)}"
        log.info("gmgn_dry_run", direction=direction, token=token_address, order_id=order_id)
        return {
            "success": True,
            "order_id": order_id,
            "signature": f"DRY_RUN_SIG_{secrets.token_hex(16)}",
            "status": "submitted",
            "direction": direction,
            "token": token_address,
            "dry_run": True,
        }
