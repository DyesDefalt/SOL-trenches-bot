"""
FeeClaimAggregator — wires PumpfunFeeClaimListener into the signal flow.

When a FeeClaimEvent arrives the aggregator:

1. Checks whether the token mint is already in our scanner candidate cache.
2. Checks whether any fee-claim shareholder is in the SmartWalletRegistry.
3. If the mint is known **or** smart shareholders are present → enriches the
   candidate dict and forwards it to the SignalEngine for scoring.
4. If the mint is unknown → optionally triggers an on-demand GeckoTerminal
   lookup to seed the candidate (configurable via *scan_unknown_mints*).
5. Maintains a rolling buffer of the last *N* raw events for Telegram
   ``/feeclaims`` inspection commands.

Integration note
----------------
Wire this into Bot startup (``src/main.py``) after the listener is created::

    listener = PumpfunFeeClaimListener(settings.helius_wss_full, ...)
    aggregator = FeeClaimAggregator(listener, signal_engine, registry)
    asyncio.create_task(listener.run())
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import TYPE_CHECKING, Any

import structlog

from src.signals.pumpfun_feeclaim import FeeClaimEvent, PumpfunFeeClaimListener

if TYPE_CHECKING:
    from src.clients.geckoterminal import GeckoTerminalClient

log = structlog.get_logger(__name__)

# How long a candidate stays in the scanner-seen cache (seconds)
_SCANNER_CACHE_TTL_S: int = 30 * 60  # 30 minutes


class FeeClaimAggregator:
    """
    Bridge between the fee-claim listener and the rest of the signal pipeline.

    Parameters
    ----------
    listener:
        A ``PumpfunFeeClaimListener`` instance (not yet started).
    signal_engine:
        Optional reference to the main SignalEngine.  When provided, enriched
        candidates are forwarded via ``signal_engine.enqueue_candidate()``.
    registry:
        Optional SmartWalletRegistry.  Enables smart-holder cross-reference.
    gecko:
        Optional GeckoTerminalClient.  Required when *scan_unknown_mints* is
        True.
    scan_unknown_mints:
        If True, mints not in the scanner cache trigger an on-demand
        GeckoTerminal lookup.  Defaults to False to avoid noisy API calls.
    recent_event_buffer_size:
        Number of raw ``FeeClaimEvent`` objects to keep for Telegram inspection.
    """

    def __init__(
        self,
        listener: PumpfunFeeClaimListener,
        signal_engine: Any | None = None,
        registry: Any | None = None,
        gecko: "GeckoTerminalClient | None" = None,
        scan_unknown_mints: bool = False,
        recent_event_buffer_size: int = 100,
    ) -> None:
        self._listener = listener
        self._signal_engine = signal_engine
        self._registry = registry
        self._gecko = gecko
        self._scan_unknown_mints = scan_unknown_mints

        # Rolling buffer of recent raw events (for Telegram /feeclaims)
        self._recent: deque[FeeClaimEvent] = deque(maxlen=recent_event_buffer_size)

        # Scanner candidate cache: mint_lower -> last_seen_ts
        # Populated externally via register_scanner_mint() or fed by the scanner.
        self._scanner_mints: dict[str, float] = {}

        # Wire callback
        self._listener._callback = self._on_fee_claim  # type: ignore[assignment]

        log.info("feeclaim_aggregator_ready", scan_unknown_mints=scan_unknown_mints)

    # ------------------------------------------------------------------
    # Scanner cache management (called by TokenScanner / SignalEngine)
    # ------------------------------------------------------------------

    def register_scanner_mint(self, mint: str) -> None:
        """
        Mark a token mint as "known" by the scanner.

        Call this whenever the scanner or signal engine processes a new
        candidate token so the aggregator can cross-reference fee claims.
        """
        self._scanner_mints[mint.lower()] = time.time()
        # Opportunistic TTL eviction (avoid unbounded growth)
        self._evict_stale_scanner_mints()

    def is_known_mint(self, mint: str) -> bool:
        """True if the mint was recently seen by the scanner."""
        ts = self._scanner_mints.get(mint.lower())
        if ts is None:
            return False
        if time.time() - ts > _SCANNER_CACHE_TTL_S:
            del self._scanner_mints[mint.lower()]
            return False
        return True

    def _evict_stale_scanner_mints(self) -> None:
        now = time.time()
        stale = [m for m, ts in self._scanner_mints.items() if now - ts > _SCANNER_CACHE_TTL_S]
        for m in stale:
            del self._scanner_mints[m]

    # ------------------------------------------------------------------
    # Event handler (registered as listener callback)
    # ------------------------------------------------------------------

    async def _on_fee_claim(self, event: FeeClaimEvent) -> None:
        """Process a decoded FeeClaimEvent from the listener."""
        self._recent.appendleft(event)

        mint_lower = event.mint.lower()
        is_known = self.is_known_mint(event.mint)

        # Smart wallet cross-reference
        smart_shareholder_count = 0
        if self._registry is not None:
            for sh in event.shareholders:
                if self._registry.is_smart_wallet(sh["pubkey"]):
                    smart_shareholder_count += 1

        has_smart_holders = smart_shareholder_count > 0

        # Decision logic
        if is_known or has_smart_holders:
            candidate = self._build_candidate(event, smart_shareholder_count)
            log.info(
                "feeclaim_candidate_emitted",
                mint=event.mint,
                distributed_sol=round(event.distributed_sol, 4),
                is_known_mint=is_known,
                smart_shareholders=smart_shareholder_count,
            )
            await self._emit_candidate(candidate)
        elif self._scan_unknown_mints and self._gecko is not None:
            log.info(
                "feeclaim_unknown_mint_on_demand",
                mint=event.mint,
                distributed_sol=round(event.distributed_sol, 4),
            )
            asyncio.create_task(
                self._on_demand_lookup(event, smart_shareholder_count)
            )
        else:
            log.debug(
                "feeclaim_no_match",
                mint=event.mint,
                distributed_sol=round(event.distributed_sol, 4),
            )

    def _build_candidate(
        self,
        event: FeeClaimEvent,
        smart_shareholder_count: int,
    ) -> dict:
        """Build enriched candidate dict from a FeeClaimEvent."""
        return {
            "address": event.mint,
            "fee_claim_signal": True,
            "fee_claim_distributed_sol": round(event.distributed_sol, 6),
            "fee_claim_smart_shareholders": smart_shareholder_count,
            "fee_claim_shareholders_total": len(event.shareholders),
            "fee_claim_signature": event.signature,
            "fee_claim_slot": event.slot,
            "fee_claim_received_at_ms": event.received_at_ms,
            # Source tag for scoring engine differentiation
            "source": "feeclaim",
            "priority": "high",
        }

    async def _emit_candidate(self, candidate: dict) -> None:
        """Forward candidate to SignalEngine if wired, else log only."""
        if self._signal_engine is None:
            log.debug("feeclaim_aggregator_no_engine", candidate=candidate)
            return
        try:
            enqueue_fn = getattr(self._signal_engine, "enqueue_candidate", None)
            if callable(enqueue_fn):
                await enqueue_fn(candidate)
            else:
                log.warning("feeclaim_aggregator_engine_missing_enqueue")
        except Exception as exc:
            log.error("feeclaim_aggregator_emit_error", error=str(exc))

    async def _on_demand_lookup(
        self,
        event: FeeClaimEvent,
        smart_shareholder_count: int,
    ) -> None:
        """
        Attempt a GeckoTerminal lookup for an unknown mint.

        If a pool is found, register the mint and emit a candidate.
        """
        if self._gecko is None:
            return
        try:
            # GeckoTerminalClient exposes get_token_pools(address)
            get_fn = getattr(self._gecko, "get_token_pools", None)
            if get_fn is None:
                log.debug("feeclaim_gecko_no_get_token_pools")
                return
            pools = await get_fn(event.mint)
        except Exception as exc:
            log.debug("feeclaim_gecko_lookup_failed", mint=event.mint, error=str(exc))
            return

        if not pools:
            log.debug("feeclaim_gecko_no_pool", mint=event.mint)
            return

        # Found a pool — register mint and emit
        self.register_scanner_mint(event.mint)
        candidate = self._build_candidate(event, smart_shareholder_count)
        candidate["gecko_pool"] = pools[0] if isinstance(pools, list) else pools
        log.info(
            "feeclaim_on_demand_candidate",
            mint=event.mint,
            distributed_sol=round(event.distributed_sol, 4),
        )
        await self._emit_candidate(candidate)

    # ------------------------------------------------------------------
    # Telegram inspection
    # ------------------------------------------------------------------

    def get_recent_events(self, limit: int = 20) -> list[FeeClaimEvent]:
        """
        Return the most recent fee-claim events (newest first).

        Used by the Telegram bot ``/feeclaims`` command for quick inspection.
        """
        return list(self._recent)[:limit]

    def get_event_for_mint(self, mint: str, lookback_ms: int = 30 * 60 * 1000) -> FeeClaimEvent | None:
        """
        Return most recent fee-claim event for a specific mint within lookback window.

        Used by SignalEngine to cross-reference candidates with fee-claim signals.
        Default lookback: 30 minutes.
        """
        import time
        cutoff_ms = int(time.time() * 1000) - lookback_ms
        for event in self._recent:
            if event.mint == mint and event.received_at_ms >= cutoff_ms:
                return event
        return None
