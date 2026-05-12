"""
Token Scanner — pull token kandidat dari berbagai source.

Sources:
1. GeckoTerminal `/networks/solana/new_pools` — token launch terbaru
2. GMGN `/api/v1/market/trending` — trending token
3. (Future) Pump.fun direct, Raydium new pools, dll

Output: list of candidate token addresses untuk di-score.

Hard filter awal (cepat, sebelum scoring):
- Skip kalau MCAP > max_mcap
- Skip kalau liquidity < min_liquidity
- Skip kalau age > 24 jam (kita target fresh launches)
- Skip kalau di blacklist
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from src.config import settings
from src.infra.logger import get_logger

if TYPE_CHECKING:
    from src.clients.geckoterminal import GeckoTerminalClient
    from src.clients.gmgn import GMGNClient

log = get_logger(__name__)


class TokenScanner:
    """Scan multiple sources untuk dapat candidate token list."""

    def __init__(
        self,
        gecko: "GeckoTerminalClient",
        gmgn: "GMGNClient | None" = None,
    ) -> None:
        self.gecko = gecko
        self.gmgn = gmgn
        self._blacklist: set[str] = set()
        self._seen_recently: dict[str, float] = {}  # address -> timestamp last seen

    def add_blacklist(self, address: str) -> None:
        self._blacklist.add(address.lower())

    async def scan(self, max_results: int = 50) -> list[dict]:
        """
        Scan + dedupe + apply hard filter.

        Returns: list of {address, symbol, mcap_usd, liquidity_usd, age_minutes, ...}

        Format normalized — backtester + live signal pakai shape yang sama.
        """
        results: list[dict] = []
        seen_addresses: set[str] = set()

        # Source 1: GeckoTerminal new pools
        try:
            new_pools = await self.gecko.get_new_pools()
            for pool in new_pools[:50]:
                token = self._extract_token_from_gecko_pool(pool)
                if token and self._is_valid_candidate(token):
                    addr_lower = token["address"].lower()
                    if addr_lower not in seen_addresses:
                        seen_addresses.add(addr_lower)
                        results.append(token)
        except Exception as e:
            log.warning("scanner_gecko_new_pools_failed", error=str(e))

        # Source 2: GeckoTerminal trending pools
        try:
            trending = await self.gecko.get_trending_pools(duration="1h")
            for pool in trending[:30]:
                token = self._extract_token_from_gecko_pool(pool)
                if token and self._is_valid_candidate(token):
                    addr_lower = token["address"].lower()
                    if addr_lower not in seen_addresses:
                        seen_addresses.add(addr_lower)
                        results.append(token)
        except Exception as e:
            log.warning("scanner_gecko_trending_failed", error=str(e))

        # Source 3: GMGN trending (kalau available)
        if self.gmgn:
            try:
                gmgn_trending = await self.gmgn.get_trending_tokens(
                    chain="sol", interval="5m", limit=20
                )
                for entry in gmgn_trending:
                    token = self._extract_token_from_gmgn(entry)
                    if token and self._is_valid_candidate(token):
                        addr_lower = token["address"].lower()
                        if addr_lower not in seen_addresses:
                            seen_addresses.add(addr_lower)
                            results.append(token)
            except Exception as e:
                log.warning("scanner_gmgn_trending_failed", error=str(e))

        log.info(
            "scan_complete",
            total_results=len(results),
            sources=["gecko_new", "gecko_trending"] + (["gmgn_trending"] if self.gmgn else []),
        )
        return results[:max_results]

    def _extract_token_from_gecko_pool(self, pool: dict) -> dict | None:
        """Extract base token info dari GeckoTerminal pool object."""
        attrs = pool.get("attributes", {})
        relationships = pool.get("relationships", {})

        # Base token address dari relationships
        base_token = relationships.get("base_token", {}).get("data", {})
        base_address = base_token.get("id", "").replace("solana_", "")

        if not base_address:
            return None

        try:
            mcap_usd = float(attrs.get("market_cap_usd") or attrs.get("fdv_usd") or 0)
            liquidity_usd = float(attrs.get("reserve_in_usd") or 0)
            volume_5m = float(attrs.get("volume_usd", {}).get("m5", 0) or 0)
            volume_1h = float(attrs.get("volume_usd", {}).get("h1", 0) or 0)
            volume_24h = float(attrs.get("volume_usd", {}).get("h24", 0) or 0)
            price_usd = float(attrs.get("base_token_price_usd") or 0)
        except (ValueError, TypeError):
            return None

        return {
            "address": base_address,
            "symbol": attrs.get("name", "").split("/")[0].strip() or "?",
            "name": attrs.get("name", ""),
            "mcap_usd": mcap_usd,
            "liquidity_usd": liquidity_usd,
            "volume_5m_usd": volume_5m,
            "volume_1h_usd": volume_1h,
            "volume_24h_usd": volume_24h,
            "price_usd": price_usd,
            "pool_address": attrs.get("address"),
            "source": "gecko",
        }

    def _extract_token_from_gmgn(self, entry: dict) -> dict | None:
        """Extract token info dari GMGN trending response."""
        try:
            return {
                "address": entry.get("address") or entry.get("token_address") or "",
                "symbol": entry.get("symbol", "?"),
                "name": entry.get("name", ""),
                "mcap_usd": float(entry.get("market_cap_usd", 0) or 0),
                "liquidity_usd": float(entry.get("liquidity_usd", 0) or 0),
                "volume_5m_usd": float(entry.get("volume_5m", 0) or 0),
                "volume_1h_usd": float(entry.get("volume_1h", 0) or 0),
                "volume_24h_usd": float(entry.get("volume_24h", 0) or 0),
                "price_usd": float(entry.get("price_usd", 0) or 0),
                "source": "gmgn",
            }
        except (ValueError, TypeError):
            return None

    def _is_valid_candidate(self, token: dict) -> bool:
        """Hard filter cepat (sebelum scoring)."""
        addr = token.get("address", "").lower()
        if not addr or addr in self._blacklist:
            return False

        # Skip stablecoin / SOL / known major tokens
        if addr in {
            "epjfwdd5aufqssqem2qn1xzybapc8g4weggkzwytdt1v",  # USDC
            "es9vmfrzacermjfrf4h2fyd4kconky11mcce8benwnyb",  # USDT
            "so11111111111111111111111111111111111111112",  # SOL
        }:
            return False

        mcap = token.get("mcap_usd", 0)
        if mcap > settings.filter_max_mcap_usd:
            return False
        if mcap > 0 and mcap < 1000:  # Too small juga skip (likely scam)
            return False

        liquidity = token.get("liquidity_usd", 0)
        if liquidity < settings.filter_min_liquidity_usd:
            return False

        return True
