"""
Signal Engine — wire scanner + multi-source intelligence + scoring → decision.

Flow per cycle (every 20-30 seconds):
1. Scanner returns list candidate tokens
2. Untuk tiap candidate (parallel-enriched):
   a. GMGN: smart money count + security info (legacy)
   b. Nansen + GMGN combined: SmartMoneyAggregator for trend + cluster
   c. TokenVerifier: 5-source safety voting
   d. PumpfunTracker: graduation status
3. Score via ScoringEngine (deterministic, 10 components now)
4. Persist signal ke DB (audit trail)
5. Kalau action=BUY → trigger entry
6. Kalau action=ALERT → send Telegram alert

Phase 7 upgrade: rich multi-source enrichment with graceful fallback.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from src.config import settings
from src.core.scoring import ScoreResult, ScoringEngine, TokenData
from src.infra.logger import get_logger

if TYPE_CHECKING:
    from src.clients.geckoterminal import GeckoTerminalClient
    from src.clients.gmgn import GMGNClient
    from src.core.scanner import TokenScanner
    from src.core.smart_wallet_registry import SmartWalletRegistry
    from src.intel.cluster_detector import ClusterDetector
    from src.intel.pumpfun_tracker import PumpfunTracker
    from src.intel.smart_money import SmartMoneyAggregator
    from src.intel.token_verifier import TokenVerifier

log = get_logger(__name__)


class SignalEngine:
    """Score live candidates dengan multi-source intelligence enrichment."""

    def __init__(
        self,
        scanner: "TokenScanner",
        gmgn: "GMGNClient",
        gecko: "GeckoTerminalClient",
        registry: "SmartWalletRegistry",
        scoring: ScoringEngine | None = None,
        smart_money_aggregator: "SmartMoneyAggregator | None" = None,
        cluster_detector: "ClusterDetector | None" = None,
        token_verifier: "TokenVerifier | None" = None,
        pumpfun_tracker: "PumpfunTracker | None" = None,
    ) -> None:
        self.scanner = scanner
        self.gmgn = gmgn
        self.gecko = gecko
        self.registry = registry
        self.scoring = scoring or ScoringEngine()

        # Phase 7 intel layer (optional — degrades gracefully)
        self.smart_money_aggregator = smart_money_aggregator
        self.cluster_detector = cluster_detector
        self.token_verifier = token_verifier
        self.pumpfun_tracker = pumpfun_tracker

    async def evaluate_cycle(self, max_candidates: int = 30) -> list[ScoreResult]:
        """One cycle: scan + enrich + score semua candidate."""
        candidates = await self.scanner.scan(max_results=max_candidates)
        log.info("signal_cycle_start", candidate_count=len(candidates))

        top_wallets = self.registry.get_top_tier_wallets(max_count=100)
        smart_addresses = [w.address for w in top_wallets]

        results: list[ScoreResult] = []
        for cand in candidates:
            try:
                token_data = await self._enrich_token(cand, smart_addresses)
                result = self.scoring.score(token_data)
                results.append(result)

                if result.action in ("BUY", "ALERT"):
                    log.info(
                        "signal_actionable",
                        token=token_data.symbol or token_data.address[:8],
                        score=result.score,
                        action=result.action,
                        sm_count=token_data.smart_money_count,
                        sm_trend=token_data.smart_money_trend,
                        cluster=token_data.cluster_signal_strength,
                        pumpfun_pct=token_data.pumpfun_graduation_pct,
                    )
            except Exception as e:
                log.warning(
                    "signal_eval_failed",
                    token=cand.get("address", "?")[:8],
                    error=str(e),
                )

        results.sort(key=lambda r: r.score, reverse=True)
        log.info(
            "signal_cycle_done",
            buy_count=sum(1 for r in results if r.action == "BUY"),
            alert_count=sum(1 for r in results if r.action == "ALERT"),
            skip_count=sum(1 for r in results if r.action == "SKIP"),
            reject_count=sum(1 for r in results if r.action == "REJECT"),
        )
        return results

    async def _enrich_token(
        self,
        candidate: dict,
        smart_addresses: list[str],
    ) -> TokenData:
        """
        Multi-source enrichment dengan parallel fetch.

        Best-effort — tiap source failure tidak block lainnya.
        """
        token = TokenData(
            address=candidate["address"],
            symbol=candidate.get("symbol", ""),
            name=candidate.get("name", ""),
            mcap_usd=candidate.get("mcap_usd", 0),
            liquidity_usd=candidate.get("liquidity_usd", 0),
            price_usd=candidate.get("price_usd", 0),
            volume_5m_usd=candidate.get("volume_5m_usd", 0),
            volume_1h_usd=candidate.get("volume_1h_usd", 0),
            volume_increasing=candidate.get("volume_5m_usd", 0) > candidate.get("volume_1h_usd", 0) / 12,
        )

        # Parallel enrichment: legacy GMGN + Phase 7 intel
        await asyncio.gather(
            self._enrich_gmgn_legacy(token, smart_addresses),
            self._enrich_smart_money(token),
            self._enrich_cluster(token),
            self._enrich_verifier(token),
            self._enrich_pumpfun(token),
            return_exceptions=True,
        )

        return token

    async def _enrich_gmgn_legacy(self, token: TokenData, smart_addresses: list[str]) -> None:
        """Legacy GMGN enrichment — smart money count + token info."""
        try:
            sm_count, sm_buyers = await self.gmgn.get_smart_money_count_for_token(
                token_address=token.address,
                smart_wallets=smart_addresses,
                minutes_lookback=15,
            )
            token.smart_money_count = sm_count
            token.smart_money_buyers = sm_buyers
        except Exception as e:
            log.debug("gmgn_sm_count_failed", token=token.address[:8], error=str(e))

        try:
            info = await self.gmgn.get_token_info(token.address)
            if info:
                token.is_honeypot = bool(info.get("is_honeypot", False))
                token.lp_burned = bool(info.get("burn_status") == "burned" or info.get("lp_burned", False))
                token.is_renounced = bool(info.get("renounced", False))
                token.gmgn_security_score = int(info.get("security_score", 0) or 0)
                token.dev_holding_pct = float(info.get("dev_holding_pct", 0) or 0)
                token.bundle_supply_pct = float(info.get("bundle_pct", 0) or 0)
                token.holder_count = int(info.get("holder_count", 0) or 0)
        except Exception as e:
            log.debug("gmgn_token_info_failed", token=token.address[:8], error=str(e))

    async def _enrich_smart_money(self, token: TokenData) -> None:
        """Nansen + GMGN smart money trend signal."""
        if not self.smart_money_aggregator or not settings.intel_nansen_trend_enabled:
            return
        try:
            signal = await self.smart_money_aggregator.get_signal(token.address, chain="sol")
            token.smart_money_trend = signal.nansen_trend
            token.smart_money_composite_bonus = signal.composite_score_bonus
        except Exception as e:
            log.debug("smart_money_enrich_failed", token=token.address[:8], error=str(e))

    async def _enrich_cluster(self, token: TokenData) -> None:
        """GMGN cluster signal — 3+ wallets buy same token in 30min."""
        if not self.cluster_detector or not settings.intel_cluster_detection_enabled:
            return
        try:
            cluster = await self.cluster_detector.get_cluster_for_token(
                token.address, chain="sol", window_minutes=30
            )
            if cluster:
                token.cluster_signal_strength = cluster.strength
            else:
                token.cluster_signal_strength = "NONE"
        except Exception as e:
            log.debug("cluster_enrich_failed", token=token.address[:8], error=str(e))

    async def _enrich_verifier(self, token: TokenData) -> None:
        """5-source multi-source safety verification."""
        if not self.token_verifier or not settings.intel_multi_source_verify_enabled:
            return
        try:
            verification = await self.token_verifier.verify(token.address, chain="sol")
            token.multi_source_safety_score = verification.weighted_safety_score
            token.multi_source_critical_flags = verification.critical_flags
        except Exception as e:
            log.debug("verifier_enrich_failed", token=token.address[:8], error=str(e))

    async def _enrich_pumpfun(self, token: TokenData) -> None:
        """Pump.fun bonding curve / graduation status."""
        if not self.pumpfun_tracker or not settings.intel_pumpfun_tracking_enabled:
            return
        try:
            status = await self.pumpfun_tracker.check(token.address)
            token.pumpfun_graduation_pct = status.graduation_pct
            token.pumpfun_score_bonus = status.score_bonus
        except Exception as e:
            log.debug("pumpfun_enrich_failed", token=token.address[:8], error=str(e))
