"""
Cross-reference validator — checks token legitimacy against external registries.

Currently supports:
- CoinGecko: market cap rank, trending status
- Messari: (optional, duck-typed — any object with get_asset(slug) method)

All sources are optional — graceful degrade to legitimacy_score=0 if unavailable.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from src.infra.logger import get_logger

log = get_logger(__name__)


@dataclass
class CrossRefResult:
    """Result from cross-reference validation across multiple registries."""

    contract_address: str
    symbol: str | None

    # CoinGecko fields
    coingecko_listed: bool = False
    coingecko_rank: int | None = None    # market_cap_rank (lower = bigger)
    coingecko_id: str | None = None

    # Messari fields
    messari_listed: bool = False
    messari_slug: str | None = None

    # Derived signals
    category: str | None = None          # if categorized on CG
    is_trending: bool = False             # in CG trending list right now
    legitimacy_score: float = 0.0        # 0-10 composite score
    cross_ref_bonus: float = 0.0         # -5 to +15 for main scoring pipeline
    reasons: list[str] = field(default_factory=list)


class CrossRefValidator:
    """
    Validate token legitimacy by cross-referencing external registries.

    Both clients are optional — pass None to skip that source gracefully.

    Usage::

        validator = CrossRefValidator(coingecko=cg_client)
        result = await validator.validate_token("So111...", symbol="SOL")
    """

    def __init__(
        self,
        coingecko: Any | None = None,   # CoinGeckoClient | None
        messari: Any | None = None,      # MessariClient | None (duck-typed)
    ) -> None:
        self._cg = coingecko
        self._messari = messari

    async def validate_token(
        self,
        contract_address: str,
        symbol: str | None = None,
        token_age_days: float | None = None,
    ) -> CrossRefResult:
        """
        Run cross-reference checks against all available registries.

        Args:
            contract_address: On-chain token mint address
            symbol: Token symbol for Messari / fallback search
            token_age_days: Age of token in days (used for suspicious-if-old penalty)

        Returns:
            CrossRefResult with legitimacy_score (0-10) and cross_ref_bonus (-5..+15)
        """
        result = CrossRefResult(
            contract_address=contract_address,
            symbol=symbol,
        )

        # Run all checks concurrently
        tasks = []
        if self._cg is not None:
            tasks.append(self._check_coingecko(result, contract_address))
        if self._messari is not None:
            tasks.append(self._check_messari(result, symbol))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # Apply scoring after all data collected
        self._compute_scores(result, token_age_days)

        log.debug(
            "crossref_validated",
            contract=contract_address,
            symbol=symbol,
            cg_listed=result.coingecko_listed,
            cg_rank=result.coingecko_rank,
            trending=result.is_trending,
            legitimacy=result.legitimacy_score,
            bonus=result.cross_ref_bonus,
        )
        return result

    # ------------------------------------------------------------------
    # Internal: per-source checks
    # ------------------------------------------------------------------

    async def _check_coingecko(
        self,
        result: CrossRefResult,
        contract_address: str,
    ) -> None:
        """Populate CoinGecko fields on result. Fail-safe."""
        try:
            data = await self._cg.get_token_by_contract(contract_address)
            if not data:
                return

            result.coingecko_listed = True
            result.coingecko_id = data.get("id")

            # Market cap rank
            rank = data.get("market_cap_rank")
            if rank is not None:
                try:
                    result.coingecko_rank = int(rank)
                except (TypeError, ValueError):
                    pass

            # Category — take first one if available
            categories = data.get("categories", [])
            if isinstance(categories, list) and categories:
                result.category = categories[0]

            # Check trending separately
            await self._check_trending(result)

        except Exception as e:
            log.warning(
                "crossref_cg_error",
                contract=contract_address,
                error=str(e),
            )

    async def _check_trending(self, result: CrossRefResult) -> None:
        """Check if token is in CoinGecko trending list."""
        try:
            trending_data = await self._cg.get_trending()
            trending_coins = trending_data.get("coins", [])
            for entry in trending_coins:
                item = entry.get("item", {}) if isinstance(entry, dict) else {}
                # Match by CoinGecko ID or symbol
                if result.coingecko_id and item.get("id") == result.coingecko_id:
                    result.is_trending = True
                    break
                if (
                    result.symbol
                    and item.get("symbol", "").upper() == result.symbol.upper()
                ):
                    result.is_trending = True
                    break
        except Exception as e:
            log.debug("crossref_trending_error", error=str(e))

    async def _check_messari(
        self,
        result: CrossRefResult,
        symbol: str | None,
    ) -> None:
        """Populate Messari fields on result. Fail-safe, duck-typed."""
        if not symbol:
            return
        try:
            # MessariClient is duck-typed — just call get_asset if available
            asset = await self._messari.get_asset(symbol.lower())
            if asset and isinstance(asset, dict) and asset.get("data"):
                result.messari_listed = True
                result.messari_slug = asset.get("data", {}).get("slug")
        except Exception as e:
            log.debug("crossref_messari_error", symbol=symbol, error=str(e))

    # ------------------------------------------------------------------
    # Scoring logic
    # ------------------------------------------------------------------

    def _compute_scores(
        self,
        result: CrossRefResult,
        token_age_days: float | None,
    ) -> None:
        """
        Apply scoring rules and populate legitimacy_score + cross_ref_bonus.

        Rules:
            CoinGecko listed:         legitimacy +5, bonus +5
            CG rank top 500:          legitimacy +5, bonus +5
            CG rank top 100:          legitimacy +10, bonus +10 (rare for memecoin)
            Messari listed:           legitimacy +3, bonus +3
            CG trending:              bonus +5 (no legitimacy change — just momentum)
            Not on either + age >7d:  bonus -3 (suspicious — should be indexed by now)
        """
        legitimacy = 0.0
        bonus = 0.0
        reasons = result.reasons

        if result.coingecko_listed:
            legitimacy += 5
            bonus += 5
            reasons.append("coingecko_listed")

            rank = result.coingecko_rank
            if rank is not None:
                if rank <= 100:
                    legitimacy += 10
                    bonus += 10
                    reasons.append(f"coingecko_top100_rank_{rank}")
                elif rank <= 500:
                    legitimacy += 5
                    bonus += 5
                    reasons.append(f"coingecko_top500_rank_{rank}")

        if result.messari_listed:
            legitimacy += 3
            bonus += 3
            reasons.append("messari_listed")

        if result.is_trending:
            bonus += 5
            reasons.append("coingecko_trending")

        # Penalty: not listed anywhere and old enough to have been indexed
        if (
            not result.coingecko_listed
            and not result.messari_listed
            and token_age_days is not None
            and token_age_days > 7
        ):
            bonus -= 3
            reasons.append("not_cross_listed_old_token")

        # Clamp: legitimacy 0-10, bonus -5 to +15
        result.legitimacy_score = max(0.0, min(10.0, legitimacy))
        result.cross_ref_bonus = max(-5.0, min(15.0, bonus))
