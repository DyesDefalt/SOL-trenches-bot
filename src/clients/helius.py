"""
Helius Solana RPC + WebSocket client.

Free tier limits (May 2026):
- 1M credits/month
- 10 RPC req/s
- 5 WebSocket connections simultaneously
- 2 credits per 0.1 MB streaming data
- WSS metering aktif mulai 1 Mei 2026

Usage:
    # RPC
    async with HeliusRPCClient() as rpc:
        balance = await rpc.get_balance(pubkey)
        sigs = await rpc.get_signatures_for_address(pubkey, limit=10)

    # WebSocket subscription
    async with HeliusWSClient() as ws:
        async for event in ws.subscribe_logs(mention=[wallet_address]):
            print(event)
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, Literal

import websockets
from websockets.client import WebSocketClientProtocol

from src.clients.base import BaseHTTPClient, HTTPError
from src.config import settings
from src.infra.logger import get_logger
from src.infra.rate_limiter import TokenBucket

log = get_logger(__name__)

Commitment = Literal["processed", "confirmed", "finalized"]


class HeliusRPCClient:
    """
    Solana JSON-RPC client melalui Helius endpoint.

    Helius adalah Solana RPC standard-compatible — semua method JSON-RPC bekerja
    sama seperti public mainnet RPC, plus DAS API tambahan untuk asset queries.
    """

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or settings.helius_api_key
        if not self.api_key:
            raise ValueError("HELIUS_API_KEY not set")

        url = settings.helius_rpc_full
        self._http = BaseHTTPClient(
            base_url=url,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=30.0,
            max_retries=3,
            force_ipv4=True,
        )

        # Free tier: 10 req/s
        self._limiter = TokenBucket(rps=10.0, burst=10.0, name="helius_rpc")
        self._request_id = 0

    async def close(self) -> None:
        await self._http.close()

    async def __aenter__(self) -> "HeliusRPCClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        """Generic JSON-RPC call. Returns 'result' field."""
        await self._limiter.acquire()

        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params or [],
        }
        response = await self._http.post("", json=payload)

        if "error" in response:
            err = response["error"]
            log.error("helius_rpc_error", method=method, error=err)
            raise HTTPError(
                f"RPC {method} error: {err.get('message')}",
                status=err.get("code"),
                body=str(err),
            )

        return response.get("result")

    # ------------------------------------------------------------------
    # Common Solana RPC methods
    # ------------------------------------------------------------------
    async def get_latest_blockhash(self, commitment: Commitment = "confirmed") -> dict[str, Any]:
        return await self.call("getLatestBlockhash", [{"commitment": commitment}])

    async def get_balance(self, pubkey: str, commitment: Commitment = "confirmed") -> int:
        """Returns lamports (1 SOL = 1e9 lamports)."""
        result = await self.call("getBalance", [pubkey, {"commitment": commitment}])
        return int(result.get("value", 0))

    async def get_account_info(
        self,
        pubkey: str,
        commitment: Commitment = "confirmed",
        encoding: Literal["base58", "base64", "jsonParsed"] = "jsonParsed",
    ) -> dict[str, Any] | None:
        result = await self.call(
            "getAccountInfo",
            [pubkey, {"commitment": commitment, "encoding": encoding}],
        )
        return result.get("value") if result else None

    async def get_signatures_for_address(
        self,
        pubkey: str,
        limit: int = 10,
        before: str | None = None,
        until: str | None = None,
        commitment: Commitment = "confirmed",
    ) -> list[dict[str, Any]]:
        """
        Get recent transaction signatures untuk wallet.

        IMPORTANT: Ada method canggih `getTransactionsForAddress` (50 credits) yang
        TIDAK tersedia di free plan. Pakai `getSignaturesForAddress` (1 credit) di sini.
        """
        params: dict[str, Any] = {"limit": limit, "commitment": commitment}
        if before:
            params["before"] = before
        if until:
            params["until"] = until

        return await self.call("getSignaturesForAddress", [pubkey, params])

    async def get_transaction(
        self,
        signature: str,
        commitment: Commitment = "confirmed",
        max_supported_transaction_version: int = 0,
    ) -> dict[str, Any] | None:
        return await self.call(
            "getTransaction",
            [
                signature,
                {
                    "commitment": commitment,
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": max_supported_transaction_version,
                },
            ],
        )

    async def get_recent_prioritization_fees(
        self,
        addresses: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Untuk dynamic priority fee calculation."""
        params: list[Any] = [addresses] if addresses else []
        return await self.call("getRecentPrioritizationFees", params)

    async def get_multiple_accounts(
        self,
        pubkeys: list[str],
        commitment: Commitment = "confirmed",
        encoding: Literal["base58", "base64", "jsonParsed"] = "jsonParsed",
    ) -> list[dict[str, Any] | None]:
        """Batch account info — hemat credit dibanding loop getAccountInfo."""
        if len(pubkeys) > 100:
            raise ValueError("max 100 accounts per call")
        result = await self.call(
            "getMultipleAccounts",
            [pubkeys, {"commitment": commitment, "encoding": encoding}],
        )
        return result.get("value", []) if result else []


class HeliusWSClient:
    """
    WebSocket subscription client untuk real-time events.

    Free tier: 5 simultaneous connections. Pakai bijak — single connection
    bisa subscribe ke banyak topics.

    Auto-reconnect dengan exponential backoff. Survive network blip.

    Usage:
        async with HeliusWSClient() as ws:
            async for event in ws.subscribe_logs(mention=[wallet_addr]):
                # handle event
                pass
    """

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or settings.helius_api_key
        if not self.api_key:
            raise ValueError("HELIUS_API_KEY not set")
        self.url = settings.helius_wss_full

        self._ws: WebSocketClientProtocol | None = None
        self._sub_id_counter = 0
        # Map WS subscription_id -> our internal queue for routing events
        self._subscriptions: dict[int, asyncio.Queue[dict[str, Any]]] = {}
        # Map our internal subscribe call ID -> WS subscription_id (returned in response)
        self._pending_subs: dict[int, asyncio.Future[int]] = {}
        self._reader_task: asyncio.Task | None = None
        self._closed = False

    async def connect(self) -> None:
        """Open WebSocket connection + start reader task."""
        if self._ws and not self._ws.closed:
            return
        log.info("helius_ws_connecting")
        self._ws = await websockets.connect(
            self.url,
            ping_interval=30,
            ping_timeout=10,
            close_timeout=5,
            max_size=10_000_000,  # 10 MB
        )
        self._reader_task = asyncio.create_task(self._reader_loop())
        log.info("helius_ws_connected")

    async def close(self) -> None:
        self._closed = True
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()

    async def __aenter__(self) -> "HeliusWSClient":
        await self.connect()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    def _next_id(self) -> int:
        self._sub_id_counter += 1
        return self._sub_id_counter

    async def _reader_loop(self) -> None:
        """Background task: read messages, route to subscriber queues."""
        backoff = 1.0
        while not self._closed:
            try:
                if not self._ws or self._ws.closed:
                    log.warning("helius_ws_disconnected_reconnecting")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    await self.connect()
                    continue

                async for raw in self._ws:
                    backoff = 1.0  # reset on successful message
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        log.warning("helius_ws_invalid_json", body=raw[:200])
                        continue
                    await self._route_message(msg)

            except websockets.ConnectionClosed:
                if self._closed:
                    return
                log.warning("helius_ws_connection_closed")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as e:
                log.error("helius_ws_reader_error", error=str(e))
                if self._closed:
                    return
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _route_message(self, msg: dict[str, Any]) -> None:
        """Route incoming WS message ke subscriber queue yang tepat."""
        # Subscribe response
        if "id" in msg and "result" in msg:
            req_id = msg["id"]
            sub_id = msg["result"]
            if req_id in self._pending_subs:
                fut = self._pending_subs.pop(req_id)
                if not fut.done():
                    fut.set_result(int(sub_id))
            return

        # Notification
        if msg.get("method", "").endswith("Notification"):
            params = msg.get("params", {})
            sub_id = params.get("subscription")
            queue = self._subscriptions.get(sub_id)
            if queue:
                await queue.put(params.get("result", {}))

        # Error
        if "error" in msg:
            log.error("helius_ws_error", error=msg["error"])

    async def _subscribe(
        self,
        method: str,
        params: list[Any],
    ) -> tuple[int, asyncio.Queue[dict[str, Any]]]:
        """Send subscribe RPC, return (sub_id, queue) for receiving events."""
        if not self._ws or self._ws.closed:
            await self.connect()
        assert self._ws is not None

        req_id = self._next_id()
        fut: asyncio.Future[int] = asyncio.get_event_loop().create_future()
        self._pending_subs[req_id] = fut

        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }
        await self._ws.send(json.dumps(payload))

        try:
            sub_id = await asyncio.wait_for(fut, timeout=10.0)
        except asyncio.TimeoutError:
            self._pending_subs.pop(req_id, None)
            raise

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._subscriptions[sub_id] = queue
        return sub_id, queue

    async def subscribe_logs(
        self,
        mention: list[str] | None = None,
        commitment: Commitment = "confirmed",
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Subscribe ke Solana logs. Filter by `mentions` array (account addresses).

        Untuk smart money tracking: mention=[wallet_address] dan kita lihat semua
        log yang menyebut wallet itu (termasuk transfer, swap, dll).

        Yields: log notification dicts dengan signature, slot, logs, err.
        """
        filter_obj: str | dict[str, Any]
        if mention:
            filter_obj = {"mentions": mention}
        else:
            filter_obj = "all"

        sub_id, queue = await self._subscribe(
            "logsSubscribe",
            [filter_obj, {"commitment": commitment}],
        )
        log.info("helius_ws_subscribed_logs", sub_id=sub_id, mention=mention)

        try:
            while True:
                event = await queue.get()
                yield event
        finally:
            # Cleanup: unsubscribe
            try:
                if self._ws and not self._ws.closed:
                    await self._ws.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": self._next_id(),
                                "method": "logsUnsubscribe",
                                "params": [sub_id],
                            }
                        )
                    )
                self._subscriptions.pop(sub_id, None)
            except Exception as e:
                log.warning("helius_ws_unsubscribe_error", error=str(e))

    async def subscribe_account(
        self,
        pubkey: str,
        commitment: Commitment = "confirmed",
        encoding: Literal["base58", "base64", "jsonParsed"] = "jsonParsed",
    ) -> AsyncIterator[dict[str, Any]]:
        """Subscribe ke account state changes."""
        sub_id, queue = await self._subscribe(
            "accountSubscribe",
            [pubkey, {"commitment": commitment, "encoding": encoding}],
        )
        log.info("helius_ws_subscribed_account", sub_id=sub_id, pubkey=pubkey)

        try:
            while True:
                event = await queue.get()
                yield event
        finally:
            try:
                if self._ws and not self._ws.closed:
                    await self._ws.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": self._next_id(),
                                "method": "accountUnsubscribe",
                                "params": [sub_id],
                            }
                        )
                    )
                self._subscriptions.pop(sub_id, None)
            except Exception as e:
                log.warning("helius_ws_unsubscribe_error", error=str(e))
