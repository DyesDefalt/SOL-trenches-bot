"""
Tests for PumpfunFeeClaimListener — Phase 10 signal source.

Coverage
--------
1.  ``_match_discriminator`` — positive match
2.  ``_match_discriminator`` — negative match (wrong prefix)
3.  ``_match_discriminator`` — data too short returns False
4.  ``_parse_program_data`` — valid "Program data: <b64>" line
5.  ``_parse_program_data`` — line without marker returns None
6.  ``_parse_program_data`` — invalid base64 returns None
7.  ``_parse_distribute_fees`` — known-good payload (2 shareholders)
8.  ``_parse_distribute_fees`` — payload too short returns None
9.  ``_parse_distribute_fees`` — shareholders_len too large (>1000) returns None
10. Deduplication: second identical event within TTL window is skipped
11. Min-fee filter: event below 0.5 SOL threshold triggers no callback
12. Full integration: _process_logs fires callback for matching log line
13. Reconnect: listener retries after WebSocket disconnect (5-s backoff)
14. ``stop()`` exits run() loop cleanly without error
15. ``_parse_distribute_fees`` — 0 shareholders is valid (empty array)

Fixture: KNOWN_PAYLOAD
----------------------
The known-good binary payload is constructed as::

    [8]  discriminator       a537817004b3ca28
    [32] mint                0101...0102  → 4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKj
    [8]  distributed u64 LE  002f685900000000  (1_500_000_000 lamports = 1.5 SOL)
    [4]  shareholders_len    02000000  (2 entries)
    [34] shareholder 1       0a0a...0a00  pubkey + 7017 (0x1770 = 6000 bps)
    [34] shareholder 2       1414...1400  pubkey + a00f (0x0fa0 = 4000 bps)

Total: 8 + 32 + 8 + 4 + 34 + 34 = 120 bytes.
"""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import time
from collections import OrderedDict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.signals.pumpfun_feeclaim import (
    DISTRIBUTE_FEES_DISCRIMINATOR,
    PUMP_AMM,
    PUMP_PROGRAM,
    FeeClaimEvent,
    PumpfunFeeClaimListener,
)

# ---------------------------------------------------------------------------
# Fixture: known-good distribute_fees binary payload
# ---------------------------------------------------------------------------

_DISCRIMINATOR = bytes.fromhex("a537817004b3ca28")

_MINT_BYTES = bytes([1] * 31 + [2])                  # → 4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKj
_HOLDER1_BYTES = bytes([10] * 31 + [0])               # → gBxS1f6uyyGPuW5MzGBukidSb71jdsCb5fZaoSzULDu
_HOLDER2_BYTES = bytes([20] * 31 + [0])               # → 2MNus2KCpxwXnp19iyXNpWSFtBD2UGjQBAL8AbtywfSo

_DISTRIBUTED_LAMPORTS = 1_500_000_000                 # 1.5 SOL
_BPS1 = 6000
_BPS2 = 4000

KNOWN_PAYLOAD: bytes = (
    _DISCRIMINATOR
    + _MINT_BYTES
    + struct.pack("<Q", _DISTRIBUTED_LAMPORTS)        # u64 LE
    + struct.pack("<I", 2)                            # shareholders_len u32 LE
    + _HOLDER1_BYTES + struct.pack("<H", _BPS1)      # shareholder 1 (32 + 2)
    + _HOLDER2_BYTES + struct.pack("<H", _BPS2)      # shareholder 2 (32 + 2)
)
assert len(KNOWN_PAYLOAD) == 120, f"Expected 120 bytes, got {len(KNOWN_PAYLOAD)}"

KNOWN_B64: str = base64.b64encode(KNOWN_PAYLOAD).decode()

KNOWN_LOG_LINE: str = f"Program data: {KNOWN_B64}"

# A wrong-discriminator payload (first byte differs)
_WRONG_DISC_PAYLOAD = bytes([0xBE]) + KNOWN_PAYLOAD[1:]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_listener(
    callback: object | None = None,
    min_fee_sol: float = 0.5,
) -> PumpfunFeeClaimListener:
    cb = callback or AsyncMock()
    return PumpfunFeeClaimListener(
        helius_ws_url="wss://fake-helius/",
        on_fee_claim_callback=cb,
        min_fee_claim_sol=min_fee_sol,
    )


# ---------------------------------------------------------------------------
# 1. _match_discriminator — positive match
# ---------------------------------------------------------------------------

def test_match_discriminator_positive():
    listener = _make_listener()
    assert listener._match_discriminator(KNOWN_PAYLOAD, DISTRIBUTE_FEES_DISCRIMINATOR) is True


# ---------------------------------------------------------------------------
# 2. _match_discriminator — negative match (wrong prefix)
# ---------------------------------------------------------------------------

def test_match_discriminator_negative():
    listener = _make_listener()
    assert listener._match_discriminator(_WRONG_DISC_PAYLOAD, DISTRIBUTE_FEES_DISCRIMINATOR) is False


# ---------------------------------------------------------------------------
# 3. _match_discriminator — data too short returns False
# ---------------------------------------------------------------------------

def test_match_discriminator_too_short():
    listener = _make_listener()
    assert listener._match_discriminator(b"\xa5\x37", DISTRIBUTE_FEES_DISCRIMINATOR) is False


# ---------------------------------------------------------------------------
# 4. _parse_program_data — valid line
# ---------------------------------------------------------------------------

def test_parse_program_data_valid():
    listener = _make_listener()
    result = listener._parse_program_data(KNOWN_LOG_LINE)
    assert result is not None
    data_bytes, prefix = result
    assert data_bytes == KNOWN_PAYLOAD
    assert "Program data: " in prefix


# ---------------------------------------------------------------------------
# 5. _parse_program_data — line without marker returns None
# ---------------------------------------------------------------------------

def test_parse_program_data_no_marker():
    listener = _make_listener()
    result = listener._parse_program_data("Program log: some message here")
    assert result is None


# ---------------------------------------------------------------------------
# 6. _parse_program_data — invalid base64 returns None
# ---------------------------------------------------------------------------

def test_parse_program_data_bad_base64():
    listener = _make_listener()
    result = listener._parse_program_data("Program data: !!!not-valid-base64!!!")
    assert result is None


# ---------------------------------------------------------------------------
# 7. _parse_distribute_fees — known-good payload (2 shareholders)
# ---------------------------------------------------------------------------

def test_parse_distribute_fees_known_good():
    listener = _make_listener()
    result = listener._parse_distribute_fees(KNOWN_PAYLOAD)

    assert result is not None
    assert result["mint"] == "4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKj"
    assert result["distributed_lamports"] == 1_500_000_000
    assert len(result["shareholders"]) == 2

    sh1, sh2 = result["shareholders"]
    assert sh1["pubkey"] == "gBxS1f6uyyGPuW5MzGBukidSb71jdsCb5fZaoSzULDu"
    assert sh1["bps"] == 6000
    assert sh2["pubkey"] == "2MNus2KCpxwXnp19iyXNpWSFtBD2UGjQBAL8AbtywfSo"
    assert sh2["bps"] == 4000


# ---------------------------------------------------------------------------
# 8. _parse_distribute_fees — payload too short returns None
# ---------------------------------------------------------------------------

def test_parse_distribute_fees_too_short():
    listener = _make_listener()
    # Only the discriminator, nothing else
    result = listener._parse_distribute_fees(_DISCRIMINATOR)
    assert result is None


# ---------------------------------------------------------------------------
# 9. _parse_distribute_fees — shareholders_len > 1000 is rejected
# ---------------------------------------------------------------------------

def test_parse_distribute_fees_huge_len():
    listener = _make_listener()
    # Build payload with shareholders_len = 5000 but no actual data after
    bad_payload = (
        _DISCRIMINATOR
        + _MINT_BYTES
        + struct.pack("<Q", 1_000_000)
        + struct.pack("<I", 5_000)   # absurd length
        # no shareholder bytes follow
    )
    result = listener._parse_distribute_fees(bad_payload)
    assert result is None


# ---------------------------------------------------------------------------
# 10. Deduplication: second identical event within TTL is skipped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deduplicate_same_event():
    callback = AsyncMock()
    listener = _make_listener(callback=callback)

    # Build a notification message matching the known payload
    notification = _make_logs_notification(
        logs=[KNOWN_LOG_LINE],
        signature="SIG_DEDUP_TEST",
        slot=100,
    )

    await listener._handle_message(notification)
    await listener._handle_message(notification)  # duplicate

    # Callback fired exactly once
    assert callback.call_count == 1


# ---------------------------------------------------------------------------
# 11. Min-fee filter: event below threshold triggers no callback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_min_fee_filter_suppresses_small_event():
    callback = AsyncMock()
    # Set threshold above 1.5 SOL — our known payload is exactly 1.5 SOL
    listener = _make_listener(callback=callback, min_fee_sol=2.0)

    notification = _make_logs_notification(
        logs=[KNOWN_LOG_LINE],
        signature="SIG_SMALL",
        slot=200,
    )
    await listener._handle_message(notification)

    callback.assert_not_called()


# ---------------------------------------------------------------------------
# 12. Full integration: _process_logs fires callback for matching log line
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_process_logs_fires_callback():
    received: list[FeeClaimEvent] = []

    async def capture(event: FeeClaimEvent) -> None:
        received.append(event)

    listener = _make_listener(callback=capture, min_fee_sol=0.5)

    await listener._process_logs(
        logs=[
            "Program log: Instruction: DistributeFees",
            KNOWN_LOG_LINE,
        ],
        signature="SIG_FULL_INTEGRATION",
        slot=300,
    )

    assert len(received) == 1
    evt = received[0]
    assert evt.mint == "4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKj"
    assert abs(evt.distributed_sol - 1.5) < 1e-6
    assert evt.signature == "SIG_FULL_INTEGRATION"
    assert evt.slot == 300
    assert len(evt.shareholders) == 2


# ---------------------------------------------------------------------------
# 13. Reconnect: run() retries after _connect_and_listen raises an exception
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reconnect_on_disconnect():
    """
    Verify that run() re-enters _connect_and_listen after a connection error
    rather than crashing.  We patch _connect_and_listen to fail once, then
    call stop() on the second attempt so run() exits cleanly.
    """
    attempt = 0

    async def _fake_connect_and_listen():
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            raise OSError("simulated connection failure")
        # Second call: stop the listener so run() exits, then return
        await listener.stop()

    callback = AsyncMock()
    listener = _make_listener(callback=callback)
    listener._connect_and_listen = _fake_connect_and_listen  # type: ignore[method-assign]

    # Patch asyncio.sleep to avoid real 5-second wait
    with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
        try:
            await asyncio.wait_for(listener.run(), timeout=3.0)
        except asyncio.TimeoutError:
            pytest.fail("run() did not exit — reconnect loop may be broken")

    assert attempt == 2, f"Expected 2 connection attempts, got {attempt}"


# ---------------------------------------------------------------------------
# 14. stop() exits run() loop cleanly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_exits_run_loop():
    """stop() should cause run() to exit gracefully (no infinite loop)."""
    callback = AsyncMock()
    listener = _make_listener(callback=callback)

    async def _fake_connect_and_listen():
        # Immediately request stop so run() exits after first iteration
        await listener.stop()

    listener._connect_and_listen = _fake_connect_and_listen  # type: ignore[method-assign]

    try:
        await asyncio.wait_for(listener.run(), timeout=3.0)
    except asyncio.TimeoutError:
        pytest.fail("run() did not exit after stop() — infinite loop suspected")


# ---------------------------------------------------------------------------
# 15. _parse_distribute_fees — 0 shareholders is valid
# ---------------------------------------------------------------------------

def test_parse_distribute_fees_zero_shareholders():
    listener = _make_listener()
    payload_no_holders = (
        _DISCRIMINATOR
        + _MINT_BYTES
        + struct.pack("<Q", 500_000_000)  # 0.5 SOL
        + struct.pack("<I", 0)            # 0 shareholders
    )
    result = listener._parse_distribute_fees(payload_no_holders)
    assert result is not None
    assert result["shareholders"] == []
    assert result["distributed_lamports"] == 500_000_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_logs_notification(
    logs: list[str],
    signature: str,
    slot: int,
    sub_id: int = 1,
) -> dict:
    """Build a Helius-style logsNotification message dict."""
    return {
        "jsonrpc": "2.0",
        "method": "logsNotification",
        "params": {
            "subscription": sub_id,
            "result": {
                "context": {"slot": slot},
                "value": {
                    "signature": signature,
                    "err": None,
                    "logs": logs,
                },
            },
        },
    }
