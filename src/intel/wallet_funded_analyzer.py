"""
Wallet Funded Analyzer — detect freshly-created wallets among top token holders.

Indonesian degen insight (@PradonoNovaldo, @badidoyo, @ELPonyin):
Sniper bots and CEX dump wallets are created moments before the token launch.
Top holders whose wallets were funded <1 day ago = coordinated snipers or
exchange cold-wallet routing — major red flag.

Algorithm:
  1. Take top 5 holders (from caller or fall back to Birdeye)
  2. For each wallet: page through getSignaturesForAddress (100 sigs/page)
     up to 3 pages (300 sigs max). Take the LAST (oldest) signature's blockTime.
  3. funding_age_days = (now - oldest_blockTime) / 86400
  4. Compute median and min across the 5 wallets.
  5. Classify by median_age_days threshold.

Pagination note:
  Helius getSignaturesForAddress returns NEWEST first.
  We paginate with `before=last_seen_sig` to reach older transactions.
  Cap at 3 pages (300 sigs) — enough to distinguish "created yesterday" from
  "active wallet months old". Unknown = not enough history returned.

Rate-limit budget:
  5 holders × 3 pages = 15 Helius calls max per analyze().
  At Helius free-tier 10 req/s this takes ~1.5s serial; parallel ~0.5s.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.infra.logger import get_logger

if TYPE_CHECKING:
    from src.clients.helius import HeliusRPCClient
    from src.intel.birdeye_client import BirdeyeClient

log = get_logger(__name__)

_SIGS_PER_PAGE = 100
_MAX_PAGES = 3  # 300 sigs cap
_SECS_PER_DAY = 86_400.0


@dataclass
class FundedFromAnalysis:
    """
    Result of wallet funding-age analysis for top holders.

    label:
      SAFE      — median wallet age > 30 days (established wallets)
      CAUTION   — median 1-30 days (relatively new)
      RED_FLAG  — median < 1 day (freshly funded snipers)
      UNKNOWN   — could not determine age for enough wallets

    median_age_days: median wallet age in days across sampled holders (None if unknown)
    min_age_days:    youngest wallet age in days (None if unknown)
    score_adjustment: contribution to composite score
    reasoning: human-readable explanation
    """

    label: str  # SAFE | CAUTION | RED_FLAG | UNKNOWN
    median_age_days: float | None
    min_age_days: float | None
    score_adjustment: float
    reasoning: str


# Score adjustments per label
_SCORE_MAP: dict[str, float] = {
    "SAFE": 3.0,
    "CAUTION": 0.0,
    "RED_FLAG": -10.0,
    "UNKNOWN": 0.0,
}


class WalletFundedAnalyzer:
    """
    Analyze how old the top holders' wallets are.

    Constructor:
        helius_rpc: HeliusRPCClient for getSignaturesForAddress.
        birdeye:    BirdeyeClient for fetching holders (optional fallback).

    Usage::

        analyzer = WalletFundedAnalyzer(helius_rpc_client, birdeye_client)
        result = await analyzer.analyze("TokenMintXxx", top_holders=["addr1", ...])
        if result.label == "RED_FLAG":
            # heavy penalty applied
    """

    def __init__(
        self,
        helius_rpc: "HeliusRPCClient",
        birdeye: "BirdeyeClient | None" = None,
    ) -> None:
        self._helius = helius_rpc
        self._birdeye = birdeye

    async def analyze(
        self,
        token_address: str,
        top_holders: list[str] | None = None,
    ) -> FundedFromAnalysis:
        """
        Assess wallet funding age for top 5 holders.

        top_holders: optional pre-fetched list of holder addresses.
                     If None and birdeye available, fetches automatically.
                     Capped at first 5 addresses.
        """
        log.info("funded_analyze_start", token=token_address)

        holders = await self._resolve_holders(token_address, top_holders)
        if not holders:
            return FundedFromAnalysis(
                label="UNKNOWN",
                median_age_days=None,
                min_age_days=None,
                score_adjustment=0.0,
                reasoning="No holder addresses available — cannot assess wallet age.",
            )

        # Limit to top 5 to avoid rate limit pressure
        sample = holders[:5]

        ages = await self._fetch_wallet_ages(sample)
        valid_ages = [a for a in ages if a is not None]

        if not valid_ages:
            return FundedFromAnalysis(
                label="UNKNOWN",
                median_age_days=None,
                min_age_days=None,
                score_adjustment=0.0,
                reasoning=(
                    f"Checked {len(sample)} wallets — could not determine oldest "
                    f"transaction for any. Wallets may be brand-new or have no history."
                ),
            )

        median_age = _median(valid_ages)
        min_age = min(valid_ages)
        label = _classify_age(median_age)
        score = _SCORE_MAP[label]

        reasoning = (
            f"Sampled {len(valid_ages)}/{len(sample)} wallets. "
            f"Median age={median_age:.1f}d, min={min_age:.1f}d → {label}."
        )

        log.info(
            "funded_analyze_done",
            token=token_address,
            label=label,
            median_days=median_age,
            min_days=min_age,
        )
        return FundedFromAnalysis(
            label=label,
            median_age_days=median_age,
            min_age_days=min_age,
            score_adjustment=score,
            reasoning=reasoning,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _resolve_holders(
        self,
        token_address: str,
        top_holders: list[str] | None,
    ) -> list[str]:
        """Return holder addresses (from arg or Birdeye)."""
        if top_holders:
            return top_holders

        if self._birdeye is None:
            return []

        try:
            raw = await self._birdeye.get_token_holders(token_address, limit=10)
            return [h.get("owner", "") for h in raw if h.get("owner")]
        except Exception as exc:  # noqa: BLE001
            log.warning("funded_birdeye_error", token=token_address, error=str(exc))
            return []

    async def _fetch_wallet_ages(self, addresses: list[str]) -> list[float | None]:
        """
        Fetch wallet age (days since oldest tx) for each address in parallel.
        """
        tasks = [self._wallet_age_days(addr) for addr in addresses]
        return list(await asyncio.gather(*tasks))

    async def _wallet_age_days(self, address: str) -> float | None:
        """
        Walk backwards through signatures to find the oldest one.

        Returns age in days since epoch, or None on failure.
        """
        oldest_block_time: int | None = None
        before_sig: str | None = None

        for page in range(_MAX_PAGES):
            try:
                sigs = await self._helius.get_signatures_for_address(
                    address,
                    limit=_SIGS_PER_PAGE,
                    before=before_sig,
                )
            except Exception as exc:  # noqa: BLE001
                log.debug(
                    "funded_sig_fetch_error",
                    address=address,
                    page=page,
                    error=str(exc),
                )
                break

            if not sigs:
                break

            # Sigs are newest-first; last element is oldest on this page
            last_sig = sigs[-1]
            block_time = last_sig.get("blockTime")
            if block_time is not None:
                oldest_block_time = int(block_time)

            if len(sigs) < _SIGS_PER_PAGE:
                # We've hit the beginning of the wallet's history
                break

            # Prepare cursor for next page (go older)
            before_sig = last_sig.get("signature")
            if not before_sig:
                break

        if oldest_block_time is None:
            return None

        age_seconds = time.time() - oldest_block_time
        return age_seconds / _SECS_PER_DAY


def _median(values: list[float]) -> float:
    """Compute median of a non-empty list."""
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    mid = n // 2
    if n % 2 == 0:
        return (sorted_vals[mid - 1] + sorted_vals[mid]) / 2.0
    return sorted_vals[mid]


def _classify_age(median_days: float) -> str:
    """Classify median wallet age into a label."""
    if median_days < 1.0:
        return "RED_FLAG"
    if median_days <= 30.0:
        return "CAUTION"
    return "SAFE"
