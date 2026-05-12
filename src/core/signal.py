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
    from src.intel.crossref_validator import CrossRefValidator
    from src.intel.macro_regime import MacroRegimeDetector
    from src.intel.news_aggregator import NewsAggregator
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
        # Phase 9: extended intelligence
        macro_detector: "MacroRegimeDetector | None" = None,
        news_aggregator: "NewsAggregator | None" = None,
        crossref_validator: "CrossRefValidator | None" = None,
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

        # Phase 9 intel layer (optional — degrades gracefully)
        self.macro_detector = macro_detector
        self.news_aggregator = news_aggregator
        self.crossref_validator = crossref_validator

        # Cached macro regime per-cycle (avoid duplicate API calls across candidates)
        self._cycle_macro: dict | None = None

    async def evaluate_cycle(self, max_candidates: int = 30) -> list[ScoreResult]:
        """One cycle: scan + enrich + score semua candidate."""
        candidates = await self.scanner.scan(max_results=max_candidates)
        log.info("signal_cycle_start", candidate_count=len(candidates))

        top_wallets = self.registry.get_top_tier_wallets(max_count=100)
        smart_addresses = [w.address for w in top_wallets]

        # Phase 9: Fetch macro regime + market sentiment once per cycle (cached upstream)
        await self._refresh_macro_context()

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

        # Phase 9: Apply cycle-level macro context to every candidate
        self._apply_macro_context(token)

        # Parallel enrichment: legacy GMGN + Phase 7 intel + Phase 9 narrative/crossref
        await asyncio.gather(
            self._enrich_gmgn_legacy(token, smart_addresses),
            self._enrich_smart_money(token),
            self._enrich_cluster(token),
            self._enrich_verifier(token),
            self._enrich_pumpfun(token),
            self._enrich_narrative(token),
            self._enrich_crossref(token),
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

    # ------------------------------------------------------------------
    # Phase 9: Macro + News + Cross-Reference enrichment
    # ------------------------------------------------------------------

    async def _refresh_macro_context(self) -> None:
        """Fetch macro regime + market sentiment once per cycle. Cached on detector side."""
        self._cycle_macro = None
        if not self.macro_detector or not settings.macro_regime_enabled:
            return
        try:
            regime = await self.macro_detector.detect_regime()
            self._cycle_macro = {
                "level": regime.level.value if hasattr(regime.level, "value") else str(regime.level),
                "multiplier": regime.position_size_multiplier,
                "skip_entries": regime.should_skip_entries,
            }
            log.info(
                "macro_regime_detected",
                level=self._cycle_macro["level"],
                multiplier=self._cycle_macro["multiplier"],
                skip_entries=self._cycle_macro["skip_entries"],
                reasons=regime.reasons[:3],
            )
        except Exception as e:
            log.debug("macro_regime_fetch_failed", error=str(e))
            self._cycle_macro = None

    def _apply_macro_context(self, token: TokenData) -> None:
        """Apply cycle-level macro regime to token."""
        if not self._cycle_macro:
            return
        token.macro_regime_level = self._cycle_macro["level"]
        token.macro_position_multiplier = self._cycle_macro["multiplier"]
        token.macro_skip_entries = self._cycle_macro["skip_entries"]

    async def _enrich_narrative(self, token: TokenData) -> None:
        """News + sentiment + FUD detection via NewsAggregator."""
        if not self.news_aggregator or not settings.news_narrative_enabled:
            return
        if not token.symbol:
            return  # need symbol for ticker lookups
        try:
            narrative = await self.news_aggregator.check_token_narrative(
                symbol=token.symbol,
                contract_address=token.address,
            )
            token.narrative_match = narrative.narrative_match
            token.narrative_bonus = narrative.narrative_bonus
            token.is_listed_on_messari = narrative.is_listed_on_messari

            # FUD detection (separate call for batch efficiency, but per-token works for low candidate counts)
            if settings.news_fud_detection_enabled:
                fud_events = await self.news_aggregator.detect_fud_events([token.symbol])
                if fud_events:
                    event = fud_events[0]
                    token.fud_detected = True
                    token.fud_severity = event.severity
        except Exception as e:
            log.debug("narrative_enrich_failed", token=token.address[:8], error=str(e))

    async def _enrich_crossref(self, token: TokenData) -> None:
        """CoinGecko + Messari legitimacy cross-reference."""
        if not self.crossref_validator or not settings.crossref_validation_enabled:
            return
        try:
            result = await self.crossref_validator.validate_token(
                contract_address=token.address,
                symbol=token.symbol or None,
            )
            token.is_listed_on_coingecko = result.coingecko_listed
            token.coingecko_rank = result.coingecko_rank
            token.crossref_bonus = result.cross_ref_bonus
        except Exception as e:
            log.debug("crossref_enrich_failed", token=token.address[:8], error=str(e))
