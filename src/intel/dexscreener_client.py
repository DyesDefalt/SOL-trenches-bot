"""
DexScreener async client (public, no key required).

DexScreener aggregates DEX trading data across 50+ chains. Each "pair" object
contains real-time price, liquidity, volume, transaction counts, and market cap.

Base URL: https://api.dexscreener.com
No authentication needed.
Rate limit: not published — 5 req/sec is safe based on community observation.

Key pair fields:
  priceUsd, liquidity.usd, volume.h24, priceChange.h24,
  txns.h24.buys, txns.h24.sells, pairCreatedAt (unix ms), dexId, chainId

Docs: https://docs.dexscreener.com/api/reference
"""

from __future__ import annotations

from src.clients.base import BaseHTTPClient, HTTPError
from src.infra.cache import cached
from src.infra.logger import get_logger
from src.infra.rate_limiter import TokenBucket

log = get_logger(__name__)


class DexscreenerClient:
    """
    Async DexScreener client.

    Usage::

        async with DexscreenerClient() as client:
            pairs = await client.get_token_pairs(token_address)
    """

    BASE_URL = "https://api.dexscreener.com"

    def __init__(self) -> None:
        self._http = BaseHTTPClient(
            base_url=self.BASE_URL,
            headers={
                "Accept": "application/json",
                "User-Agent": "solana-sniper-bot/0.1",
            },
            timeout=15.0,
            max_retries=3,
        )
        self._limiter = TokenBucket(rps=5.0, burst=10.0, name="dexscreener")

    async def close(self) -> None:
        await self._http.close()

    async def __aenter__(self) -> "DexscreenerClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        await self._limiter.acquire()
        return await self._http.get(path, params=params)

    # ------------------------------------------------------------------
    # Core endpoints
    # ------------------------------------------------------------------

    @cached(prefix="dexscreener:token_pairs", ttl=30)
    async def get_token_pairs(self, token_address: str) -> list[dict]:
        """
        All pairs for a token across all DEXes and chains.

        Returns list of pair objects sorted by liquidity (highest first
        from the API).

        Cached 30s — pair data is volatile during active trading.
        """
        try:
            result = await self._get(f"/latest/dex/tokens/{token_address}")
            return result.get("pairs", []) if isinstance(result, dict) else []
        except HTTPError as e:
            log.error(
                "dexscreener_token_pairs_error",
                token=token_address,
                status=e.status,
                error=str(e),
            )
            return []

    @cached(prefix="dexscreener:pair", ttl=30)
    async def get_pair(self, chain: str, pair_address: str) -> dict:
        """
        Single pair by chain + pair contract address.

        Useful for tracking a specific pool after initial discovery.
        """
        try:
            result = await self._get(f"/latest/dex/pairs/{chain}/{pair_address}")
            if isinstance(result, dict):
                pairs = result.get("pairs", [])
                return pairs[0] if pairs else {}
            return {}
        except HTTPError as e:
            log.error(
                "dexscreener_pair_error",
                chain=chain,
                pair=pair_address,
                status=e.status,
                error=str(e),
            )
            return {}

    @cached(prefix="dexscreener:search", ttl=30)
    async def search(self, query: str) -> list[dict]:
        """
        Full-text search for tokens/pairs by name, symbol, or address.

        Returns list of pair objects.
        """
        try:
            result = await self._get("/latest/dex/search", params={"q": query})
            return result.get("pairs", []) if isinstance(result, dict) else []
        except HTTPError as e:
            log.error(
                "dexscreener_search_error",
                query=query,
                status=e.status,
                error=str(e),
            )
            return []

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def get_top_pair_for_token(
        self,
        token_address: str,
        chain: str = "solana",
    ) -> dict | None:
        """
        Return the Solana pair with the highest USD liquidity for a token.

        Filters to `chain` first, falls back to all pairs if none found.
        Returns None if no pairs exist.
        """
        pairs = await self.get_token_pairs(token_address)
        if not pairs:
            return None

        # Filter to requested chain
        chain_pairs = [p for p in pairs if p.get("chainId", "").lower() == chain.lower()]
        candidates = chain_pairs if chain_pairs else pairs

        # Sort by liquidity.usd descending — higher liquidity = more reliable price
        def liquidity_usd(pair: dict) -> float:
            liq = pair.get("liquidity") or {}
            return float(liq.get("usd", 0) or 0)

        candidates_sorted = sorted(candidates, key=liquidity_usd, reverse=True)
        return candidates_sorted[0] if candidates_sorted else None
