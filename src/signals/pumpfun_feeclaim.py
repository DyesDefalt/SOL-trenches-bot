"""
Pump.fun Fee-Claim WebSocket Listener — Phase 10 Signal Source 4.

Subscribes to Pump.fun program logs via Helius WebSocket.  When the on-chain
``distribute_fees`` instruction fires the listener decodes the Anchor event
payload, applies noise filters, deduplicates, and emits a ``FeeClaimEvent``
via callback.

Programs monitored
------------------
- Pump.fun bonding curve: ``6EF8rrecthR5DkzonNwu78hRvfCKubJ14M5uBEwF6P``
- Pump.fun AMM:           ``pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA``

Event encoding
--------------
Helius ``logsSubscribe`` delivers raw log lines exactly as Solana runtime
produces them.  Anchor writes events as ``Program data: <base64>`` lines
inside program log sections.  The base64 payload is::

    [8 bytes discriminator][32 bytes mint pubkey][8 bytes u64 LE distributed]
    [4 bytes u32 LE shareholders_len][shareholders_len × (32 bytes pubkey + 2 bytes u16 LE bps)]

The discriminator for ``distribute_fees`` is ``a537817004b3ca28`` (hex).
This matches Anchor's sha256("event:DistributeFees")[..8] convention.

Helius log delivery note
------------------------
Helius passes **all** log lines for a matching transaction, not just lines
emitted by the subscribed program.  So the listener needs to verify the
discriminator prefix rather than rely on the program header alone.

Reconnect behaviour
-------------------
On disconnect the listener waits ``_RECONNECT_BACKOFF_S`` seconds (default 5)
before reconnecting.  In-flight subscription IDs become stale after disconnect;
the listener always sends fresh ``logsSubscribe`` requests after re-connecting.
"""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

import websockets
from solders.pubkey import Pubkey
import structlog

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PUMP_PROGRAM = "6EF8rrecthR5DkzonNwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"

DISTRIBUTE_FEES_DISCRIMINATOR = bytes.fromhex("a537817004b3ca28")

_LAMPORTS_PER_SOL: float = 1_000_000_000.0
_RECONNECT_BACKOFF_S: float = 5.0
_DEDUPE_TTL_MS: int = 10 * 60 * 1_000  # 10 minutes in milliseconds
_DEDUPE_MAX_KEYS: int = 10_000  # cap memory usage


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class FeeClaimEvent:
    """Decoded distribute_fees Anchor event emitted by Pump.fun programs."""

    mint: str                      # base58 token mint address
    distributed_sol: float         # total fee distributed (SOL)
    shareholders: list[dict]       # each: {"pubkey": str, "bps": int}
    signature: str                 # transaction signature
    slot: int                      # confirmed slot
    received_at_ms: int            # wall-clock ms when listener received the event

    def dedupe_key(self) -> str:
        """Stable key used for 10-minute deduplication window."""
        distributed_lamports = int(self.distributed_sol * _LAMPORTS_PER_SOL)
        return f"{self.signature}:{self.mint}:{distributed_lamports}"


# ---------------------------------------------------------------------------
# Listener
# ---------------------------------------------------------------------------

class PumpfunFeeClaimListener:
    """
    Subscribe to Pump.fun program logs, decode fee-distribution events, and
    invoke *on_fee_claim_callback* for each qualifying event.

    Parameters
    ----------
    helius_ws_url:
        Full Helius WebSocket URL including ``?api-key=`` query parameter.
    on_fee_claim_callback:
        Async callable receiving a single ``FeeClaimEvent`` argument.
    min_fee_claim_sol:
        Events below this threshold are dropped as noise (default 0.5 SOL).
    """

    def __init__(
        self,
        helius_ws_url: str,
        on_fee_claim_callback: Callable[[FeeClaimEvent], Awaitable[None]],
        min_fee_claim_sol: float = 0.5,
    ) -> None:
        self._url = helius_ws_url
        self._callback = on_fee_claim_callback
        self.min_fee_claim_sol = min_fee_claim_sol

        self._running = False
        self._stopped = False

        # Ordered dict for TTL-based deduplication: key -> received_at_ms
        self._seen: OrderedDict[str, int] = OrderedDict()

        # Request-ID counter for JSON-RPC messages
        self._req_id: int = 0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        Main loop.

        Connect → subscribe to both programs → process messages → on disconnect
        wait ``_RECONNECT_BACKOFF_S`` and reconnect.  Exits cleanly when
        ``stop()`` is called.
        """
        self._running = True
        log.info("feeclaim_listener_starting", programs=[PUMP_PROGRAM, PUMP_AMM])

        while self._running:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                log.info("feeclaim_listener_cancelled")
                break
            except Exception as exc:
                if not self._running:
                    break
                log.warning(
                    "feeclaim_listener_disconnected",
                    error=str(exc),
                    reconnect_in_s=_RECONNECT_BACKOFF_S,
                )
                await asyncio.sleep(_RECONNECT_BACKOFF_S)

        log.info("feeclaim_listener_stopped")

    async def stop(self) -> None:
        """Signal the listener to exit after the current iteration."""
        self._running = False
        self._stopped = True
        log.info("feeclaim_listener_stop_requested")

    # ------------------------------------------------------------------
    # Internal: connection + subscription lifecycle
    # ------------------------------------------------------------------

    async def _connect_and_listen(self) -> None:
        """Open one WS connection, subscribe to both programs, process messages."""
        async with websockets.connect(
            self._url,
            ping_interval=30,
            ping_timeout=10,
            close_timeout=5,
            max_size=10_000_000,
        ) as ws:
            log.info("feeclaim_ws_connected")

            # Subscribe to both programs; collect assigned subscription IDs
            pump_sub_id = await self._subscribe_logs(ws, PUMP_PROGRAM)
            amm_sub_id = await self._subscribe_logs(ws, PUMP_AMM)

            log.info(
                "feeclaim_subscribed",
                pump_sub_id=pump_sub_id,
                amm_sub_id=amm_sub_id,
            )

            # Message loop
            async for raw in ws:
                if not self._running:
                    break
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    log.debug("feeclaim_ws_bad_json", snippet=str(raw)[:80])
                    continue
                await self._handle_message(msg)

    async def _subscribe_logs(self, ws: Any, program: str) -> int:
        """
        Send a ``logsSubscribe`` request and wait for the subscription ID.

        Returns the integer subscription ID assigned by the RPC node.
        """
        self._req_id += 1
        req_id = self._req_id
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [program]},
                {"commitment": "confirmed"},
            ],
        }
        await ws.send(json.dumps(payload))

        # Wait for the subscription confirmation (may arrive before data msgs)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                msg = json.loads(raw)
            except (asyncio.TimeoutError, json.JSONDecodeError):
                continue

            if msg.get("id") == req_id and "result" in msg:
                return int(msg["result"])

            # Not our confirmation — queue it for normal processing later
            await self._handle_message(msg)

        raise RuntimeError(f"logsSubscribe confirmation timeout for program {program}")

    # ------------------------------------------------------------------
    # Internal: message processing
    # ------------------------------------------------------------------

    async def _handle_message(self, msg: dict) -> None:
        """Dispatch a single decoded WS message."""
        if "error" in msg:
            log.warning("feeclaim_ws_rpc_error", error=msg["error"])
            return

        # Notifications have method == "logsNotification"
        if msg.get("method") != "logsNotification":
            return

        params = msg.get("params", {})
        result = params.get("result", {})
        value = result.get("value", {})
        logs: list[str] = value.get("logs") or []
        signature: str = value.get("signature", "")
        slot: int = result.get("context", {}).get("slot", 0)

        if not logs:
            return

        await self._process_logs(logs, signature, slot)

    async def _process_logs(
        self,
        logs: list[str],
        signature: str,
        slot: int,
    ) -> None:
        """
        Scan log lines for ``Program data:`` entries, attempt to decode each as
        a ``distribute_fees`` event.
        """
        for line in logs:
            parsed = self._parse_program_data(line)
            if parsed is None:
                continue

            data_bytes, _prefix = parsed
            if not self._match_discriminator(data_bytes, DISTRIBUTE_FEES_DISCRIMINATOR):
                continue

            event_dict = self._parse_distribute_fees(data_bytes)
            if event_dict is None:
                continue

            distributed_sol = event_dict["distributed_lamports"] / _LAMPORTS_PER_SOL

            # Noise filter
            if distributed_sol < self.min_fee_claim_sol:
                log.debug(
                    "feeclaim_below_min",
                    mint=event_dict["mint"],
                    distributed_sol=distributed_sol,
                    min_sol=self.min_fee_claim_sol,
                )
                continue

            event = FeeClaimEvent(
                mint=event_dict["mint"],
                distributed_sol=distributed_sol,
                shareholders=event_dict["shareholders"],
                signature=signature,
                slot=slot,
                received_at_ms=int(time.time() * 1000),
            )

            # Deduplication
            key = event.dedupe_key()
            if self._is_duplicate(key):
                log.debug("feeclaim_duplicate_skipped", key=key)
                continue
            self._record_seen(key, event.received_at_ms)

            log.info(
                "feeclaim_event",
                mint=event.mint,
                distributed_sol=round(distributed_sol, 4),
                shareholders=len(event.shareholders),
                signature=signature[:16],
                slot=slot,
            )

            try:
                await self._callback(event)
            except Exception as exc:
                log.error("feeclaim_callback_error", error=str(exc), mint=event.mint)

    # ------------------------------------------------------------------
    # Internal: parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_program_data(line: str) -> tuple[bytes, str] | None:
        """
        Extract raw bytes from a ``Program data: <base64>`` log line.

        Returns ``(decoded_bytes, prefix_str)`` or ``None`` if the line does
        not match.  The prefix is the text before the base64 payload (useful
        for diagnostics).
        """
        marker = "Program data: "
        idx = line.find(marker)
        if idx == -1:
            return None

        prefix = line[:idx + len(marker)]
        b64_part = line[idx + len(marker):].strip()

        # Anchor sometimes pads with trailing '=' — Python's b64decode handles it
        try:
            decoded = base64.b64decode(b64_part)
        except Exception:
            return None

        if len(decoded) < 8:
            # Too short to contain a discriminator
            return None

        return decoded, prefix

    @staticmethod
    def _match_discriminator(data: bytes, target: bytes) -> bool:
        """Return True iff the first 8 bytes of *data* equal *target*."""
        return len(data) >= 8 and data[:8] == target

    @staticmethod
    def _parse_distribute_fees(data: bytes) -> dict | None:
        """
        Parse the ``distribute_fees`` Anchor event payload.

        Expected binary layout (little-endian)::

            Offset  Size  Type     Field
            ------  ----  -------  -----
             0       8    u8[8]    discriminator (already matched, skip)
             8      32    pubkey   mint
            40       8    u64      distributed_lamports
            48       4    u32      shareholders array length
            52+      variable     shareholders:
                      32    pubkey   shareholder address
                       2    u16      basis points (bps, 0-10000)

        Returns a dict or None on any parse error.
        """
        try:
            offset = 8  # skip discriminator

            if len(data) < offset + 32 + 8 + 4:
                log.debug("feeclaim_parse_too_short", length=len(data))
                return None

            # mint pubkey (32 bytes)
            mint_bytes = data[offset:offset + 32]
            offset += 32
            mint = str(Pubkey.from_bytes(mint_bytes))

            # distributed lamports (u64 LE)
            (distributed_lamports,) = struct.unpack_from("<Q", data, offset)
            offset += 8

            # shareholders array length (u32 LE)
            (shareholders_len,) = struct.unpack_from("<I", data, offset)
            offset += 4

            # Sanity cap: reject absurdly large arrays (malformed data)
            if shareholders_len > 1_000:
                log.debug(
                    "feeclaim_parse_shareholders_len_unreasonable",
                    shareholders_len=shareholders_len,
                )
                return None

            shareholders: list[dict] = []
            for _ in range(shareholders_len):
                if len(data) < offset + 32 + 2:
                    log.debug("feeclaim_parse_shareholder_truncated", offset=offset)
                    return None

                holder_bytes = data[offset:offset + 32]
                offset += 32
                holder_pubkey = str(Pubkey.from_bytes(holder_bytes))

                (bps,) = struct.unpack_from("<H", data, offset)
                offset += 2

                shareholders.append({"pubkey": holder_pubkey, "bps": bps})

            return {
                "mint": mint,
                "distributed_lamports": distributed_lamports,
                "shareholders": shareholders,
            }

        except struct.error as exc:
            log.debug("feeclaim_parse_struct_error", error=str(exc))
            return None
        except Exception as exc:
            log.debug("feeclaim_parse_unexpected", error=str(exc))
            return None

    # ------------------------------------------------------------------
    # Internal: deduplication
    # ------------------------------------------------------------------

    def _is_duplicate(self, key: str) -> bool:
        """True if this key was already seen within the TTL window."""
        if key not in self._seen:
            return False
        seen_at = self._seen[key]
        now_ms = int(time.time() * 1000)
        if now_ms - seen_at > _DEDUPE_TTL_MS:
            # Expired entry — remove and treat as new
            del self._seen[key]
            return False
        return True

    def _record_seen(self, key: str, received_at_ms: int) -> None:
        """Record a key as seen; evict expired entries to bound memory."""
        self._seen[key] = received_at_ms

        # Evict oldest entries that have passed TTL
        now_ms = int(time.time() * 1000)
        stale = [k for k, ts in self._seen.items() if now_ms - ts > _DEDUPE_TTL_MS]
        for k in stale:
            self._seen.pop(k, None)

        # Hard cap — evict oldest by insertion order
        while len(self._seen) > _DEDUPE_MAX_KEYS:
            self._seen.popitem(last=False)
