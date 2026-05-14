"""
Top Holder Balance Checker — assess SOL balance of top token holders.

Indonesian degen insight (@PradonoNovaldo, @badidoyo, @ELPonyin):
A serious sniper / smart money player has skin in the game — they hold real
SOL. If a top holder of your target token has < 0.2 SOL in their wallet,
they are likely a throwaway bot wallet that will dump the moment it moons.
Multiple weak-balance holders = coordinated weak sniper activity.

Algorithm:
  1. Fetch SOL balance for top 2-8 holders via Helius getBalance
  2. Count wallets with balance < 0.2 SOL (weak threshold)
  3. Compute min and median balance across all sampled holders
  4. Classify: STRONG if all > 0.5 SOL, WEAK if ≥3 weak holders, else MIXED

Score contributions:
  STRONG: +5  (all holders well-funded → conviction buys)
  MIXED:   0  (neutral)
  WEAK:   -5  (multiple throwaway wallets → likely coordinated dump)
  UNKNOWN: 0  (no data)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.infra.logger import get_logger

if TYPE_CHECKING:
    from src.clients.helius import HeliusRPCClient
    from src.intel.birdeye_client import BirdeyeClient

log = get_logger(__name__)

_LAMPORTS_PER_SOL = 1_000_000_000
_WEAK_THRESHOLD_SOL = 0.2    # below this = weak holder
_STRONG_THRESHOLD_SOL = 0.5  # all above this = STRONG


@dataclass
class HolderBalanceAnalysis:
    """
    Result of SOL balance check on top token holders.

    label:
      STRONG  — all sampled holders have >0.5 SOL (conviction buyers)
      MIXED   — mix of funded and weak wallets
      WEAK    — 3+ holders have <0.2 SOL (sniper dump risk)
      UNKNOWN — no balance data available

    min_balance_sol:    lowest SOL balance among sampled holders
    median_balance_sol: median SOL balance
    weak_count:         number of holders with <0.2 SOL
    score_adjustment:   contribution to composite score (-5 to +5)
    reasoning:          human-readable explanation
    """

    label: str  # STRONG | MIXED | WEAK | UNKNOWN
    min_balance_sol: float | None
    median_balance_sol: float | None
    weak_count: int
    score_adjustment: float
    reasoning: str


# Score adjustments per label
_SCORE_MAP: dict[str, float] = {
    "STRONG": 5.0,
    "MIXED": 0.0,
    "WEAK": -5.0,
    "UNKNOWN": 0.0,
}


class TopHolderBalanceChecker:
    """
    Check SOL balances of top token holders.

    Constructor:
        helius_rpc: HeliusRPCClient for getBalance calls.
        birdeye:    BirdeyeClient (optional) — used to fetch holders when not provided.

    Usage::

        checker = TopHolderBalanceChecker(helius_rpc_client, birdeye_client)
        result = await checker.check("TokenMintXxx", top_holders=["addr1", ...])
        if result.label == "WEAK":
            # apply penalty
    """

    def __init__(
        self,
        helius_rpc: "HeliusRPCClient",
        birdeye: "BirdeyeClient | None" = None,
    ) -> None:
        self._helius = helius_rpc
        self._birdeye = birdeye

    async def check(
        self,
        token_address: str,
        top_holders: list[str] | None = None,
    ) -> HolderBalanceAnalysis:
        """
        Check SOL balances for top 2-8 holders.

        top_holders: pre-fetched list of holder addresses.
                     If None and birdeye available, fetched automatically.
                     Capped at first 8 addresses.
        """
        log.info("holder_balance_check_start", token=token_address)

        holders = await self._resolve_holders(token_address, top_holders)
        if not holders:
            return HolderBalanceAnalysis(
                label="UNKNOWN",
                min_balance_sol=None,
                median_balance_sol=None,
                weak_count=0,
                score_adjustment=0.0,
                reasoning="No holder addresses available — cannot assess SOL balances.",
            )

        # Cap at 8 to stay within Helius free-tier budget
        sample = holders[:8]

        balances = await self._fetch_balances(sample)
        valid = [b for b in balances if b is not None]

        if not valid:
            return HolderBalanceAnalysis(
                label="UNKNOWN",
                min_balance_sol=None,
                median_balance_sol=None,
                weak_count=0,
                score_adjustment=0.0,
                reasoning=f"getBalance returned no data for {len(sample)} holders.",
            )

        weak_count = sum(1 for b in valid if b < _WEAK_THRESHOLD_SOL)
        min_bal = min(valid)
        median_bal = _median(valid)

        label = _classify(valid, weak_count)
        score = _SCORE_MAP[label]

        reasoning = (
            f"Sampled {len(valid)}/{len(sample)} holders. "
            f"Min={min_bal:.3f} SOL, median={median_bal:.3f} SOL, "
            f"weak (<{_WEAK_THRESHOLD_SOL} SOL): {weak_count} → {label}."
        )

        log.info(
            "holder_balance_check_done",
            token=token_address,
            label=label,
            min_sol=min_bal,
            median_sol=median_bal,
            weak_count=weak_count,
        )
        return HolderBalanceAnalysis(
            label=label,
            min_balance_sol=min_bal,
            median_balance_sol=median_bal,
            weak_count=weak_count,
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
        """Return holder addresses from arg or Birdeye."""
        if top_holders:
            return top_holders

        if self._birdeye is None:
            return []

        try:
            raw = await self._birdeye.get_token_holders(token_address, limit=10)
            return [h.get("owner", "") for h in raw if h.get("owner")]
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "holder_balance_birdeye_error",
                token=token_address,
                error=str(exc),
            )
            return []

    async def _fetch_balances(self, addresses: list[str]) -> list[float | None]:
        """Fetch SOL balance for each address in parallel."""
        tasks = [self._get_sol_balance(addr) for addr in addresses]
        return list(await asyncio.gather(*tasks))

    async def _get_sol_balance(self, address: str) -> float | None:
        """Return SOL balance for one address, or None on error."""
        try:
            lamports = await self._helius.get_balance(address)
            return lamports / _LAMPORTS_PER_SOL
        except Exception as exc:  # noqa: BLE001
            log.debug("holder_balance_error", address=address, error=str(exc))
            return None


def _median(values: list[float]) -> float:
    """Compute median of a non-empty list."""
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    mid = n // 2
    if n % 2 == 0:
        return (sorted_vals[mid - 1] + sorted_vals[mid]) / 2.0
    return sorted_vals[mid]


def _classify(balances: list[float], weak_count: int) -> str:
    """
    Classify holder balance profile.

    STRONG: all sampled balances > STRONG_THRESHOLD (0.5 SOL)
    WEAK:   3 or more weak holders (< 0.2 SOL)
    MIXED:  everything else
    """
    if all(b > _STRONG_THRESHOLD_SOL for b in balances):
        return "STRONG"
    if weak_count >= 3:
        return "WEAK"
    return "MIXED"
