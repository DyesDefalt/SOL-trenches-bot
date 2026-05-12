"""
Birdeye API async client (Solana DeFi analytics).

Birdeye provides token overview, security checks, holder data, and price feeds.
Free-tier public endpoints work without a key; premium endpoints require
X-API-KEY header.

Base URL: https://public-api.birdeye.so
Key header: X-API-KEY (set BIRDEYE_API_KEY env var for premium access)
Chain header: solana (always sent)

Rate limit: ~50 RPM on free tier. We use 0.8 req/sec (= 48/min) to stay safe.

Docs: https://docs.birdeye.so
"""

from __future__ import annotations

import os

from src.clients.base import BaseHTTPClient, HTTPError
from src.config import settings
from src.infra.cache import cached
from src.infra.logger import get_logger
from src.infra.rate_limiter import TokenBucket

log = get_logger(__name__)

_KEY_WARNING_EMITTED = False


class BirdeyeClient:
    """
    Async Birdeye client.

    Usage::

        async with BirdeyeClient() as client:
            overview = await client.get_token_overview(token_address)
    """

    BASE_URL = "https://public-api.birdeye.so"

    def __init__(self, api_key: str | None = None) -> None:
        global _KEY_WARNING_EMITTED  # noqa: PLW0603

        key = api_key or settings.birdeye_api_key or os.environ.get("BIRDEYE_API_KEY", "")
        if not key and not _KEY_WARNING_EMITTED:
            log.warning(
                "birdeye_no_api_key",
                note="Running without API key — some endpoints may return 401. "
                     "Set BIRDEYE_API_KEY for premium access.",
            )
            _KEY_WARNING_EMITTED = True

        headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": "solana-sniper-bot/0.1",
            "x-chain": "solana",
        }
        if key:
            headers["X-API-KEY"] = key

        self._http = BaseHTTPClient(
            base_url=self.BASE_URL,
            headers=headers,
            timeout=15.0,
            max_retries=3,
        )
        # Conservative: 0.8 req/sec (= 48 RPM) to stay within free-tier 50 RPM
        self._limiter = TokenBucket(rps=0.8, burst=5.0, name="birdeye")

    async def close(self) -> None:
        await self._http.close()

    async def __aenter__(self) -> "BirdeyeClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        await self._limiter.acquire()
        return await self._http.get(path, params=params)

    # ------------------------------------------------------------------
    # Token overview
    # ------------------------------------------------------------------

    @cached(prefix="birdeye:overview", ttl=60)
    async def get_token_overview(self, token_address: str) -> dict:
        """
        Full token overview: price, market cap, volume, liquidity, fdv.

        Requires API key for premium fields.
        """
        try:
            result = await self._get(
                "/defi/token_overview",
                params={"address": token_address},
            )
            return result.get("data", result)
        except HTTPError as e:
            log.error("birdeye_overview_error", token=token_address, status=e.status)
            return {}

    # ------------------------------------------------------------------
    # Token security
    # ------------------------------------------------------------------

    @cached(prefix="birdeye:security", ttl=300)
    async def get_token_security(self, token_address: str) -> dict:
        """
        Security report: mint authority, freeze authority, owner concentration.

        Cached 300s — security state rarely changes quickly.
        """
        try:
            result = await self._get(
                "/defi/token_security",
                params={"address": token_address},
            )
            return result.get("data", result)
        except HTTPError as e:
            log.error("birdeye_security_error", token=token_address, status=e.status)
            return {}

    # ------------------------------------------------------------------
    # Token holders
    # ------------------------------------------------------------------

    @cached(prefix="birdeye:holders", ttl=60)
    async def get_token_holders(
        self,
        token_address: str,
        limit: int = 20,
    ) -> list[dict]:
        """
        Top token holders list.

        Returns list of {owner, amount, ui_amount, ui_amount_string, rank}.
        """
        try:
            result = await self._get(
                "/defi/token_holder",
                params={"address": token_address, "limit": limit},
            )
            data = result.get("data", result)
            if isinstance(data, dict):
                return data.get("items", data.get("holders", []))
            return data if isinstance(data, list) else []
        except HTTPError as e:
            log.error("birdeye_holders_error", token=token_address, status=e.status)
            return []

    # ------------------------------------------------------------------
    # Price
    # ------------------------------------------------------------------

    @cached(prefix="birdeye:price", ttl=60)
    async def get_token_price(self, token_address: str) -> dict:
        """
        Current token price in USD.

        Returns {value: float, updateUnixTime: int, updateHumanTime: str}.
        """
        try:
            result = await self._get(
                "/defi/price",
                params={"address": token_address},
            )
            return result.get("data", result)
        except HTTPError as e:
            log.error("birdeye_price_error", token=token_address, status=e.status)
            return {}
