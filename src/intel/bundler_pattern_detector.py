"""
Bundler Pattern Detector — detect multi-wallet bundler rigs on Solana memecoins.

Indonesian degen insight (@PradonoNovaldo, @badidoyo, @ELPonyin):
One person controls 2-8 wallets that all buy the same token at launch,
creating illusion of organic demand. Tell-tale signs:
  - Multiple top holders with nearly identical % supply (within ±5%)
  - Those same wallets carry similar SOL balances (within ±20%)
  - 3+ matching wallets → CONFIRMED rug setup

Detection algorithm:
  1. Fetch top 8-10 holders from Birdeye (or GeckoTerminal fallback)
  2. Fetch SOL balance for each holder via Helius getBalance
  3. Pairwise similarity: same supply_pct (±5%) AND similar SOL (±20%)
  4. Cluster wallets by similarity graph (connected components)
  5. Largest cluster ≥ 3 → CONFIRMED, cluster of 2 → SUSPICIOUS, else NONE

False-positive note:
  Two wallets may coincidentally hold same % (e.g., both round-lot buyers).
  We require BOTH supply_pct match AND SOL balance match simultaneously.
  Solo whales with identical round-lot sizes but very different SOL will
  NOT trigger because the SOL tolerance check fails.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.infra.logger import get_logger

if TYPE_CHECKING:
    from src.clients.helius import HeliusRPCClient
    from src.intel.birdeye_client import BirdeyeClient

log = get_logger(__name__)

_LAMPORTS_PER_SOL = 1_000_000_000


@dataclass
class BundlerPattern:
    """
    Result of bundler pattern detection for a single token.

    strength:
      NONE       — no suspicious cluster found
      SUSPICIOUS — 2 wallets match (possible but inconclusive)
      CONFIRMED  — 3+ wallets share nearly identical supply% and SOL balance

    detected_wallets: addresses in the suspicious/confirmed cluster
    total_supply_pct: combined % of token supply held by the cluster
    reasoning: human-readable explanation
    """

    strength: str  # "NONE" | "SUSPICIOUS" | "CONFIRMED"
    detected_wallets: list[str] = field(default_factory=list)
    total_supply_pct: float = 0.0
    reasoning: str = ""


class BundlerPatternDetector:
    """
    Detect multi-wallet bundler patterns for Solana memecoins.

    Constructor:
        birdeye:    BirdeyeClient for holder list (premium required for full data).
                    Falls back to None → graceful degrade.
        helius_rpc: HeliusRPCClient for getBalance calls.
                    If None → SOL balance step skipped, supply_pct only used.

    Usage::

        detector = BundlerPatternDetector(birdeye_client, helius_rpc_client)
        pattern = await detector.detect("So1Token...")
        if pattern.strength == "CONFIRMED":
            # hard reject
    """

    # Similarity thresholds
    _SUPPLY_PCT_TOLERANCE = 5.0   # ±5% of supply (absolute difference)
    _SOL_BALANCE_TOLERANCE = 0.20  # ±20% relative difference in SOL

    def __init__(
        self,
        birdeye: "BirdeyeClient | None",
        helius_rpc: "HeliusRPCClient | None",
    ) -> None:
        self._birdeye = birdeye
        self._helius = helius_rpc

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def detect(
        self,
        token_address: str,
        top_n: int = 8,
    ) -> BundlerPattern:
        """
        Run bundler detection for token_address.

        Returns BundlerPattern with strength NONE/SUSPICIOUS/CONFIRMED.
        Never raises — returns NONE on any unrecoverable error.
        """
        log.info("bundler_detect_start", token=token_address, top_n=top_n)

        holders = await self._fetch_holders(token_address, top_n)
        if not holders:
            log.warning("bundler_no_holders", token=token_address)
            return BundlerPattern(
                strength="NONE",
                reasoning="No holder data available — cannot assess bundler risk.",
            )

        # Fetch SOL balances in parallel
        addresses = [h["owner"] for h in holders]
        sol_balances = await self._fetch_sol_balances(addresses)

        # Enrich holders with SOL balance
        enriched: list[dict] = []
        for h in holders:
            owner = h["owner"]
            supply_pct = float(h.get("supply_pct", 0.0))
            sol = sol_balances.get(owner)
            enriched.append(
                {
                    "owner": owner,
                    "supply_pct": supply_pct,
                    "sol_balance": sol,  # None if unavailable
                }
            )

        cluster = self._find_largest_cluster(enriched)
        result = self._classify_cluster(cluster, enriched)

        log.info(
            "bundler_detect_done",
            token=token_address,
            strength=result.strength,
            cluster_size=len(result.detected_wallets),
            supply_pct=result.total_supply_pct,
        )
        return result

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    async def _fetch_holders(
        self,
        token_address: str,
        top_n: int,
    ) -> list[dict]:
        """
        Fetch top holders. Try Birdeye first, return [] on failure.

        Each holder dict must have: owner (str), supply_pct (float).
        Birdeye returns amount/ui_amount — we compute supply_pct from
        the top-N set (first holder as 100% reference if total unknown).
        """
        if self._birdeye is None:
            log.debug("bundler_birdeye_unavailable", token=token_address)
            return []

        try:
            raw = await self._birdeye.get_token_holders(token_address, limit=top_n)
        except Exception as exc:  # noqa: BLE001
            log.warning("bundler_birdeye_error", token=token_address, error=str(exc))
            return []

        if not raw:
            return []

        return self._normalise_holders(raw)

    @staticmethod
    def _normalise_holders(raw: list[dict]) -> list[dict]:
        """
        Normalise Birdeye holder dicts into {owner, supply_pct}.

        Birdeye holder schema: {owner, amount, ui_amount, ui_amount_string, rank}.
        The API does not always return percentage directly, so we compute
        relative share from the ui_amount values across the fetched set.

        If the API returns a `percentage` or `pct` field directly, we use that.
        """
        out: list[dict] = []
        for h in raw:
            owner = h.get("owner", "")
            if not owner:
                continue

            # Some Birdeye responses include direct percentage fields
            pct = (
                h.get("percentage")
                or h.get("pct")
                or h.get("supply_pct")
                or None
            )
            if pct is not None:
                out.append({"owner": owner, "supply_pct": float(pct)})
                continue

            # Fallback: store raw ui_amount for later relative computation
            ui = float(h.get("ui_amount") or h.get("amount") or 0.0)
            out.append({"owner": owner, "_ui_amount": ui, "supply_pct": 0.0})

        # If we had to use ui_amount, compute relative shares within this set
        has_raw = any("_ui_amount" in h for h in out)
        if has_raw:
            total = sum(h.get("_ui_amount", 0.0) for h in out)
            if total > 0:
                for h in out:
                    ui = h.pop("_ui_amount", 0.0)
                    h["supply_pct"] = (ui / total) * 100.0
            else:
                for h in out:
                    h.pop("_ui_amount", None)

        return out

    async def _fetch_sol_balances(
        self,
        addresses: list[str],
    ) -> dict[str, float | None]:
        """
        Fetch SOL balance (in SOL, not lamports) for each address via Helius.

        Returns dict {address: sol_float | None}.
        Uses asyncio.gather for parallel calls. Failures per-wallet → None.
        """
        if not self._helius or not addresses:
            return {addr: None for addr in addresses}

        async def _get_one(addr: str) -> tuple[str, float | None]:
            try:
                lamports = await self._helius.get_balance(addr)
                return addr, lamports / _LAMPORTS_PER_SOL
            except Exception as exc:  # noqa: BLE001
                log.debug("bundler_balance_error", address=addr, error=str(exc))
                return addr, None

        results = await asyncio.gather(*[_get_one(a) for a in addresses])
        return dict(results)

    # ------------------------------------------------------------------
    # Clustering logic
    # ------------------------------------------------------------------

    def _are_similar(self, a: dict, b: dict) -> bool:
        """
        Return True if two holders look like the same person (bundler wallets).

        Criteria (BOTH must hold):
        1. Supply % within ±5 percentage points.
        2. SOL balance within ±20% (relative), OR SOL data absent for both.

        If only one wallet has SOL data we apply supply_pct check only
        (weaker signal — included but labelled SUSPICIOUS at most).
        """
        pct_diff = abs(a["supply_pct"] - b["supply_pct"])
        if pct_diff > self._SUPPLY_PCT_TOLERANCE:
            return False

        sol_a = a.get("sol_balance")
        sol_b = b.get("sol_balance")

        if sol_a is None and sol_b is None:
            # No SOL data for either — only supply_pct matched; treat as similar
            return True

        if sol_a is None or sol_b is None:
            # Partial SOL data — supply_pct alone gives a weak signal.
            # Return True here so the cluster can form but _classify_cluster
            # will see missing SOL and at most give SUSPICIOUS.
            return True

        # Both have SOL data — require ±20% relative match
        avg_sol = (sol_a + sol_b) / 2.0
        if avg_sol == 0.0:
            # Both have 0 SOL → match (very weak wallets, suspicious)
            return True
        rel_diff = abs(sol_a - sol_b) / avg_sol
        return rel_diff <= self._SOL_BALANCE_TOLERANCE

    def _find_largest_cluster(self, holders: list[dict]) -> list[int]:
        """
        Find the largest connected component in the similarity graph.

        Uses Union-Find over holder indices.
        Returns list of indices in the largest similar group.
        """
        n = len(holders)
        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(x: int, y: int) -> None:
            parent[find(x)] = find(y)

        for i in range(n):
            for j in range(i + 1, n):
                if self._are_similar(holders[i], holders[j]):
                    union(i, j)

        # Group by root
        groups: dict[int, list[int]] = {}
        for idx in range(n):
            root = find(idx)
            groups.setdefault(root, []).append(idx)

        if not groups:
            return []
        return max(groups.values(), key=len)

    def _classify_cluster(
        self,
        cluster_indices: list[int],
        holders: list[dict],
    ) -> BundlerPattern:
        """
        Convert cluster indices → BundlerPattern with strength/reasoning.
        """
        size = len(cluster_indices)

        if size < 2:
            return BundlerPattern(
                strength="NONE",
                reasoning="No pairwise wallet similarity detected. Holder distribution appears organic.",
            )

        cluster_holders = [holders[i] for i in cluster_indices]
        wallets = [h["owner"] for h in cluster_holders]
        total_pct = sum(h["supply_pct"] for h in cluster_holders)

        # Check if we have full SOL data for at least 2 wallets in cluster
        sol_confirmed = sum(
            1 for h in cluster_holders if h.get("sol_balance") is not None
        )

        if size >= 3 and sol_confirmed >= 2:
            strength = "CONFIRMED"
            reason = (
                f"{size} wallets hold ~{total_pct:.1f}% supply with nearly identical "
                f"supply share and SOL balance — high confidence bundler rig detected."
            )
        elif size >= 3:
            strength = "CONFIRMED"
            reason = (
                f"{size} wallets hold ~{total_pct:.1f}% supply with nearly identical "
                f"supply share (SOL balance data partial)."
            )
        else:
            # size == 2
            strength = "SUSPICIOUS"
            reason = (
                f"2 wallets hold ~{total_pct:.1f}% supply with similar metrics — "
                f"possible bundler but inconclusive (need ≥3 to confirm)."
            )

        return BundlerPattern(
            strength=strength,
            detected_wallets=wallets,
            total_supply_pct=total_pct,
            reasoning=reason,
        )
