"""
CoinGecko API async client for cross-reference validation.

Supports both Demo tier (CG- prefix keys) and Pro tier keys.
Auth detection:
  - Key starts with "CG-" → Demo URL + x-cg-demo-api-key header
  - Otherwise → Pro URL + x-cg-pro-api-key header

Rate limit: Demo = 30 req/min → TokenBucket(rps=0.5, burst=3)

All methods are fail-safe: return {} or [] on error.
404 for contract lookup = token not yet on CG (expected for new memecoins).
"""

from __future__ import annotations

import os

from src.clients.base import BaseHTTPClient, HTTPError
from src.config import settings
from src.infra.cache import cached
from src.infra.logger import get_logger
from src.infra.rate_limiter import TokenBucket

log = get_logger(__name__)

_DEMO_BASE_URL = "https://api.coingecko.com/api/v3"
_PRO_BASE_URL = "https://pro-api.coingecko.com/api/v3"


class CoinGeckoClient:
    """
    Async CoinGecko client for token cross-reference validation.

    Usage::

        async with CoinGeckoClient() as client:
            data = await client.get_token_by_contract("So11111111111111111111111111111111111111112")
    """

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        key = (
            api_key
            or settings.coingecko_api_key
            or os.environ.get("COINGECKO_API_KEY", "")
        )

        # Auto-detect tier from key prefix
        if key.startswith("CG-"):
            resolved_base = base_url or _DEMO_BASE_URL
            auth_header = "x-cg-demo-api-key"
        elif key:
            resolved_base = base_url or _PRO_BASE_URL
            auth_header = "x-cg-pro-api-key"
        else:
            # No key — public endpoints only (very limited)
            resolved_base = base_url or _DEMO_BASE_URL
            auth_header = ""

        headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": "solana-sniper-bot/0.1",
        }
        if key and auth_header:
            headers[auth_header] = key

        self._http = BaseHTTPClient(
            base_url=resolved_base,
            headers=headers,
            timeout=15.0,
            max_retries=2,
        )
        # Demo tier: 30 RPM → 0.5 req/sec, burst 3
        self._limiter = TokenBucket(rps=0.5, burst=3.0, name="coingecko")

    async def close(self) -> None:
        await self._http.close()

    async def __aenter__(self) -> "CoinGeckoClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        await self._limiter.acquire()
        return await self._http.get(path, params=params)

    # ------------------------------------------------------------------
    # Contract lookup (primary cross-ref method)
    # ------------------------------------------------------------------

    @cached(prefix="coingecko:contract", ttl=3600)
    async def get_token_by_contract(
        self,
        contract_address: str,
        platform: str = "solana",
    ) -> dict:
        """
        Look up a token by contract address on a specific platform.

        Returns full token data or {} if not listed on CoinGecko.
        404 = token not yet indexed (expected for new memecoins — silent {}).
        """
        try:
            return await self._get(f"/coins/{platform}/contract/{contract_address}")
        except HTTPError as e:
            if e.status == 404:
                # Not listed — expected for new memecoins, no noise
                log.debug(
                    "coingecko_contract_not_listed",
                    contract=contract_address,
                    platform=platform,
                )
            elif e.status == 401:
                log.error(
                    "coingecko_auth_error",
                    status=e.status,
                    note="Invalid or missing API key",
                )
            elif e.status == 429:
                log.warning(
                    "coingecko_rate_limit_persistent",
                    contract=contract_address,
                )
            else:
                log.error(
                    "coingecko_contract_error",
                    contract=contract_address,
                    status=e.status,
                )
            return {}

    # ------------------------------------------------------------------
    # Coin ID lookup
    # ------------------------------------------------------------------

    @cached(prefix="coingecko:coin", ttl=600)
    async def get_token_by_id(self, coin_id: str) -> dict:
        """
        Fetch full coin details by CoinGecko ID.

        Returns {} on any error.
        """
        try:
            return await self._get(
                f"/coins/{coin_id}",
                params={
                    "localization": "false",
                    "tickers": "false",
                    "community_data": "false",
                    "developer_data": "false",
                },
            )
        except HTTPError as e:
            if e.status == 401:
                log.error("coingecko_auth_error", status=e.status)
            elif e.status == 429:
                log.warning("coingecko_rate_limit_persistent", coin_id=coin_id)
            else:
                log.error("coingecko_coin_error", coin_id=coin_id, status=e.status)
            return {}

    # ------------------------------------------------------------------
    # Trending
    # ------------------------------------------------------------------

    @cached(prefix="coingecko:trending", ttl=600)
    async def get_trending(self) -> dict:
        """
        Return top 7 trending coins from the last 24h.

        Structure: {"coins": [{"item": {...}}, ...], "nfts": [...], "categories": [...]}
        """
        try:
            return await self._get("/search/trending")
        except HTTPError as e:
            if e.status == 401:
                log.error("coingecko_auth_error", status=e.status)
            else:
                log.error("coingecko_trending_error", status=e.status)
            return {}

    # ------------------------------------------------------------------
    # Simple price
    # ------------------------------------------------------------------

    @cached(prefix="coingecko:price", ttl=60)
    async def get_simple_price(
        self,
        coin_ids: list[str],
        vs_currencies: list[str] | None = None,
    ) -> dict:
        """
        Fetch current prices for a list of coin IDs.

        Returns {coin_id: {currency: price}} or {} on error.
        """
        if vs_currencies is None:
            vs_currencies = ["usd"]
        try:
            return await self._get(
                "/simple/price",
                params={
                    "ids": ",".join(coin_ids),
                    "vs_currencies": ",".join(vs_currencies),
                },
            )
        except HTTPError as e:
            if e.status == 401:
                log.error("coingecko_auth_error", status=e.status)
            else:
                log.error("coingecko_price_error", status=e.status)
            return {}

    # ------------------------------------------------------------------
    # Solana categories
    # ------------------------------------------------------------------

    @cached(prefix="coingecko:categories", ttl=3600)
    async def get_solana_categories(self) -> list:
        """
        Return coin categories sorted by market cap (descending).

        Useful for identifying Solana memecoin category trends.
        """
        try:
            result = await self._get(
                "/coins/categories",
                params={"order": "market_cap_desc"},
            )
            return result if isinstance(result, list) else []
        except HTTPError as e:
            if e.status == 401:
                log.error("coingecko_auth_error", status=e.status)
            else:
                log.error("coingecko_categories_error", status=e.status)
            return []

    # ------------------------------------------------------------------
    # Search by symbol / name
    # ------------------------------------------------------------------

    @cached(prefix="coingecko:search", ttl=600)
    async def search(self, query: str) -> dict:
        """
        Search CoinGecko by symbol or name.

        Returns {"coins": [...], "exchanges": [...], ...} or {} on error.
        """
        try:
            return await self._get("/search", params={"query": query})
        except HTTPError as e:
            if e.status == 401:
                log.error("coingecko_auth_error", status=e.status)
            else:
                log.error("coingecko_search_error", query=query, status=e.status)
            return {}
