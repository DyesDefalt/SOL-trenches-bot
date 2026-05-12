"""Tests for GMGNSwapClient."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.clients.gmgn_swap_client import GMGNSwapClient

TOKEN = "So11111111111111111111111111111111111111111"
WALLET = "11111111111111111111111111111112"


@pytest.fixture
def client() -> GMGNSwapClient:
    return GMGNSwapClient(wallet_address=WALLET)


# ------------------------------------------------------------------
# DRY_RUN tests (no subprocess executed)
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buy_dry_run(client: GMGNSwapClient) -> None:
    """buy_token in DRY_RUN mode returns fake response without subprocess."""
    with patch("src.clients.gmgn_swap_client.settings") as mock_settings:
        mock_settings.dry_run = True
        result = await client.buy_token(token_address=TOKEN, sol_amount=0.01)

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["order_id"].startswith("DRY_RUN_")
    assert result["signature"].startswith("DRY_RUN_SIG_")
    assert result["direction"] == "buy"


@pytest.mark.asyncio
async def test_sell_dry_run(client: GMGNSwapClient) -> None:
    """sell_token in DRY_RUN mode returns fake response without subprocess."""
    with patch("src.clients.gmgn_swap_client.settings") as mock_settings:
        mock_settings.dry_run = True
        result = await client.sell_token(token_address=TOKEN, token_amount_smallest=1_000_000)

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["order_id"].startswith("DRY_RUN_")
    assert result["direction"] == "sell"


@pytest.mark.asyncio
async def test_buy_dry_run_unique_order_ids(client: GMGNSwapClient) -> None:
    """Each DRY_RUN call produces a different order_id."""
    with patch("src.clients.gmgn_swap_client.settings") as mock_settings:
        mock_settings.dry_run = True
        r1 = await client.buy_token(token_address=TOKEN, sol_amount=0.01)
        r2 = await client.buy_token(token_address=TOKEN, sol_amount=0.01)

    assert r1["order_id"] != r2["order_id"]


# ------------------------------------------------------------------
# Subprocess mock tests (non-dry-run)
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buy_subprocess_success(client: GMGNSwapClient) -> None:
    """buy_token calls subprocess and parses JSON stdout."""
    fake_output = json.dumps({
        "success": True,
        "order_id": "ord_abc123",
        "signature": "5xSIG",
        "status": "submitted",
    }).encode()

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(fake_output, b""))

    with (
        patch("src.clients.gmgn_swap_client.settings") as mock_settings,
        patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
    ):
        mock_settings.dry_run = False
        result = await client.buy_token(token_address=TOKEN, sol_amount=0.02)

    assert result["success"] is True
    assert result["order_id"] == "ord_abc123"
    assert result["signature"] == "5xSIG"
    # Verify subprocess was called with key args
    call_args = mock_exec.call_args[0]
    assert "gmgn-cli" in call_args
    assert "swap" in call_args
    assert "--anti-mev" in call_args
    assert TOKEN in call_args


@pytest.mark.asyncio
async def test_condition_orders_json_serialization(client: GMGNSwapClient) -> None:
    """condition_orders are serialized as JSON string in the subprocess command."""
    fake_output = json.dumps({"success": True, "order_id": "ord_x", "signature": "SIG"}).encode()

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(fake_output, b""))

    orders = [
        {"order_type": "profit_stop", "side": "sell", "price_scale": "80", "sell_ratio": "30"},
        {"order_type": "loss_stop", "side": "sell", "price_scale": "45", "sell_ratio": "100"},
    ]

    with (
        patch("src.clients.gmgn_swap_client.settings") as mock_settings,
        patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
    ):
        mock_settings.dry_run = False
        await client.buy_token(
            token_address=TOKEN,
            sol_amount=0.01,
            condition_orders=orders,
        )

    call_args = list(mock_exec.call_args[0])
    # --condition-orders flag must be present with JSON payload
    assert "--condition-orders" in call_args
    idx = call_args.index("--condition-orders")
    parsed = json.loads(call_args[idx + 1])
    assert len(parsed) == 2
    assert parsed[0]["order_type"] == "profit_stop"


@pytest.mark.asyncio
async def test_buy_subprocess_nonzero_exit(client: GMGNSwapClient) -> None:
    """Non-zero exit code from subprocess returns success=False."""
    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.communicate = AsyncMock(return_value=(b"", b"some error output"))

    with (
        patch("src.clients.gmgn_swap_client.settings") as mock_settings,
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
    ):
        mock_settings.dry_run = False
        result = await client.buy_token(token_address=TOKEN, sol_amount=0.01)

    assert result["success"] is False
    assert "exit_code_1" in result["error"]
