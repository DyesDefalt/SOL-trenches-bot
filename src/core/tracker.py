"""
Smart Money Live Tracker — subscribe Helius WebSocket ke top tier wallet.

Free tier: 5 WebSocket connections. Kita pakai 1 connection dengan multiple
logsSubscribe (mention=[wallet1, wallet2, ...]). Tracker emit event saat ada
log yang menyebut wallet smart money.

Kombinasi tracker + signal engine: tracker emit "SmartWalletBuyEvent" untuk
trigger signal cycle dini (tidak nunggu interval scanner). Hot path = fastest.

Untuk MVP: tracker hanya log + cache `recent_smart_money_buys` di Redis. Signal
engine tetap pakai polling GMGN untuk official count (karena parsing log Solana
butuh decoder yang kompleks).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from src.infra.logger import get_logger

if TYPE_CHECKING:
    from src.clients.helius import HeliusWSClient
    from src.core.smart_wallet_registry import SmartWalletRegistry
    from src.infra.cache import Cache

log = get_logger(__name__)


class SmartMoneyTracker:
    """Track smart wallet activity via Helius WebSocket."""

    def __init__(
        self,
        ws: "HeliusWSClient",
        registry: "SmartWalletRegistry",
        cache: "Cache",
        max_wallets: int = 100,
    ) -> None:
        self.ws = ws
        self.registry = registry
        self.cache = cache
        self.max_wallets = max_wallets
        self._running = False

    async def run(self) -> None:
        """
        Main loop: subscribe ke top tier wallet, emit events.

        Run via:
            asyncio.create_task(tracker.run())
        """
        top = self.registry.get_top_tier_wallets(max_count=self.max_wallets)
        if not top:
            log.warning("tracker_no_wallets", note="run bootstrap_smart_wallets first")
            return

        addresses = [w.address for w in top]
        log.info("tracker_subscribing", wallet_count=len(addresses))

        self._running = True
        try:
            async for event in self.ws.subscribe_logs(mention=addresses):
                await self._handle_event(event, addresses)
                if not self._running:
                    break
        except asyncio.CancelledError:
            log.info("tracker_cancelled")
            raise
        except Exception as e:
            log.error("tracker_failed", error=str(e))

    async def stop(self) -> None:
        self._running = False

    async def _handle_event(self, event: dict, watch_addresses: list[str]) -> None:
        """
        Process single log notification.

        Untuk MVP minimal: cache event timestamp per wallet. Signal engine bisa
        query "wallet X aktif <X menit terakhir?" untuk priority scoring.

        Future enhancement: parse log untuk detect "swap" instruction, extract
        token address yang dibeli — emit SmartWalletBuyEvent ke event bus.
        """
        sig = event.get("value", {}).get("signature") or event.get("signature", "")
        slot = event.get("context", {}).get("slot") or event.get("slot")
        logs = event.get("value", {}).get("logs") or event.get("logs", [])

        # Match wallet — kita punya signature, perlu lookup yang mana wallet kita
        # involve. Pakai logs string match (cepat, tidak perlu RPC call):
        watch_set = {addr.lower() for addr in watch_addresses}
        active_wallets: set[str] = set()
        for log_line in logs:
            log_lower = log_line.lower()
            for addr in watch_set:
                if addr in log_lower:
                    active_wallets.add(addr)

        if not active_wallets:
            return

        # Cache event ke Redis dengan TTL 15 menit (signal scoring window)
        ts = datetime.now(timezone.utc).timestamp()
        for addr in active_wallets:
            await self.cache.set(
                f"sm_active:{addr}",
                {
                    "timestamp": ts,
                    "signature": sig,
                    "slot": slot,
                },
                ttl=900,  # 15 menit
            )

        log.debug(
            "tracker_event",
            active_wallets=len(active_wallets),
            signature=sig[:16] if sig else "",
        )

    async def is_recently_active(self, wallet_address: str, max_age_seconds: int = 900) -> bool:
        """Check apakah wallet aktif dalam X detik terakhir (dari cache)."""
        cached = await self.cache.get(f"sm_active:{wallet_address.lower()}")
        if not cached:
            return False
        age = datetime.now(timezone.utc).timestamp() - cached.get("timestamp", 0)
        return age <= max_age_seconds
