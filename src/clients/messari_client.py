"""
Messari API async client.

Messari provides crypto asset profiles, metrics, and news aggregation.
Used for cross-referencing token fundamentals and finding news by asset slug.

⚠️ Endpoint migration (verified May 2026 against docs.messari.io):
- Host changed: `data.messari.io` -> `api.messari.io`
- Path changed: `/api/v1/assets/{slug}/...` -> `/metrics/v1/assets/{slug}`
- The new v1 single-asset endpoint returns category, contract addresses,
  description, marketData, links, etc. inline — no `fields=` filter needed.
- v2 list endpoint: `/metrics/v2/assets` (used for `find_slug_by_contract`).
- News service appears to live under a different path (`/news/v1/...`); the
  old `/news` endpoint is deprecated. We keep get_news methods returning []
  as graceful degrade until the new path is confirmed by the user's plan.

Base URL: https://api.messari.io
Auth: x-messari-api-key header
Rate limit: 20 req/min free tier -> TokenBucket(rps=0.3, burst=3)

Docs: https://docs.messari.io/api-reference/authentication
"""

from __future__ import annotations

from src.clients.base import BaseHTTPClient, HTTPError
from src.config import settings
from src.infra.cache import cached
from src.infra.logger import get_logger
from src.infra.rate_limiter import TokenBucket

log = get_logger(__name__)


class MessariClient:
    """
    Async Messari client.

    Usage::

        async with MessariClient() as client:
            profile = await client.get_asset_profile("solana")
    """

    BASE_URL = "https://api.messari.io"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or settings.messari_api_key

        headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": "solana-sniper-bot/0.1",
        }
        if self._api_key:
            headers["x-messari-api-key"] = self._api_key

        self._http = BaseHTTPClient(
            base_url=self.BASE_URL,
            headers=headers,
            timeout=20.0,
            max_retries=2,
        )
        # Free tier: 20 req/min → 0.33 rps. Use 0.3 rps with burst=3
        self._limiter = TokenBucket(rps=0.3, burst=3, name="messari")

    async def close(self) -> None:
        await self._http.close()

    async def __aenter__(self) -> "MessariClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        await self._limiter.acquire()
        return await self._http.get(path, params=params)

    # ------------------------------------------------------------------
    # Asset profile
    # ------------------------------------------------------------------

    @cached(prefix="messari:profile", ttl=600)
    async def get_asset_profile(self, slug: str) -> dict:
        """
        Fetch asset profile including category and contract addresses.

        slug: e.g. "solana", "bonk", "dogwifhat"
        Returns profile dict or {} on error / 404.

        Migrated to v1 metrics endpoint — returns category, contractAddresses,
        marketData, links, etc. inline. No fields= filter needed; Messari
        returns the full asset payload by default.
        """
        try:
            result = await self._get(f"/metrics/v1/assets/{slug}")
            return result.get("data", result)
        except HTTPError as e:
            if e.status == 404:
                log.debug("messari_asset_not_found", slug=slug)
            elif e.status in (401, 403):
                log.warning("messari_auth_error", slug=slug, status=e.status)
            else:
                log.error("messari_profile_error", slug=slug, status=e.status)
            return {}
        except Exception as e:
            log.error("messari_profile_exception", slug=slug, error=str(e))
            return {}

    # ------------------------------------------------------------------
    # Asset metrics
    # ------------------------------------------------------------------

    @cached(prefix="messari:metrics", ttl=600)
    async def get_asset_metrics(self, slug: str) -> dict:
        """
        Fetch asset market metrics, on-chain data, and exchange flows.

        Returns metrics dict or {} on error.

        Migration note: the dedicated `/assets/{slug}/metrics` endpoint is
        gone in the new API — marketData is now embedded inside the
        single-asset response from `/metrics/v1/assets/{slug}`. We call that
        same endpoint and extract the `marketData` field. Callers expecting
        the old wrapper shape may need to read `data["marketData"]` instead
        of `data` directly.
        """
        try:
            result = await self._get(f"/metrics/v1/assets/{slug}")
            data = result.get("data", result)
            # Old endpoint returned just the metrics dict; the new one
            # nests marketData inside the full asset payload. Return that
            # sub-object if present, else the whole payload (caller decides).
            if isinstance(data, dict) and "marketData" in data:
                return data.get("marketData") or {}
            return data
        except HTTPError as e:
            if e.status == 404:
                log.debug("messari_metrics_not_found", slug=slug)
            elif e.status in (401, 403):
                log.warning("messari_auth_error", slug=slug, status=e.status)
            else:
                log.error("messari_metrics_error", slug=slug, status=e.status)
            return {}
        except Exception as e:
            log.error("messari_metrics_exception", slug=slug, error=str(e))
            return {}

    # ------------------------------------------------------------------
    # News
    # ------------------------------------------------------------------

    @cached(prefix="messari:news", ttl=600)
    async def get_news(self, limit: int = 20) -> list[dict]:
        """
        Fetch general crypto news feed.

        Returns list of news items or [] on error.
        """
        params = {
            "fields": "id,title,published_at,author,references",
            "page": 1,
        }
        try:
            result = await self._get("/news", params=params)
            data = result.get("data", [])
            if isinstance(data, list):
                return data[:limit]
            return []
        except HTTPError as e:
            if e.status in (401, 403):
                log.warning("messari_auth_error_news", status=e.status)
            else:
                log.error("messari_news_error", status=e.status)
            return []
        except Exception as e:
            log.error("messari_news_exception", error=str(e))
            return []

    # ------------------------------------------------------------------
    # Asset-specific news
    # ------------------------------------------------------------------

    @cached(prefix="messari:asset_news", ttl=600)
    async def get_asset_news(self, slug: str, limit: int = 10) -> list[dict]:
        """
        Fetch news for a specific asset by slug.

        Returns list of news items or [] on error.
        """
        try:
            result = await self._get(f"/news/{slug}")
            data = result.get("data", [])
            if isinstance(data, list):
                return data[:limit]
            return []
        except HTTPError as e:
            if e.status == 404:
                log.debug("messari_asset_news_not_found", slug=slug)
            elif e.status in (401, 403):
                log.warning("messari_auth_error", slug=slug, status=e.status)
            else:
                log.error("messari_asset_news_error", slug=slug, status=e.status)
            return []
        except Exception as e:
            log.error("messari_asset_news_exception", slug=slug, error=str(e))
            return []

    # ------------------------------------------------------------------
    # Contract-to-slug lookup
    # ------------------------------------------------------------------

    @cached(prefix="messari:contract_slug", ttl=86400)
    async def find_slug_by_contract(self, contract_address: str) -> str | None:
        """
        Find Messari slug for a token given its contract address.

        Uses v2 assets list endpoint with `search` query (Messari's search
        matches against slug/symbol/name/contract per docs.messari.io). The
        legacy `fields=` filter is gone in v2 — we fetch standard listing
        fields and check `contractAddresses` locally.

        Cached 24h since contract→slug mapping is stable.
        Returns slug string or None if not found.
        """
        # v2 endpoint accepts `search` (matches contract too) + `limit`.
        # We pass the contract directly so Messari narrows the list server-side.
        params: dict = {
            "search": contract_address,
            "limit": 20,
        }
        try:
            result = await self._get("/metrics/v2/assets", params=params)
            data = result.get("data", [])
            if not isinstance(data, list):
                return None

            contract_lower = contract_address.lower()
            for asset in data:
                # v2 schema flattens contractAddresses onto the asset; old
                # v1 nested it under profile.contract_addresses. Support both
                # shapes so we don't lose lookups if Messari adjusts the schema.
                contract_addrs = (
                    asset.get("contractAddresses")
                    or (asset.get("profile") or {}).get("contract_addresses")
                    or []
                )
                if not isinstance(contract_addrs, list):
                    continue
                for entry in contract_addrs:
                    if not isinstance(entry, dict):
                        continue
                    # v2 uses `contractAddress` (camelCase); v1 used `contract_address`.
                    addr = (
                        entry.get("contractAddress")
                        or entry.get("contract_address")
                        or ""
                    ).lower()
                    if addr == contract_lower:
                        return asset.get("slug")
            return None
        except HTTPError as e:
            if e.status in (401, 403):
                log.warning("messari_auth_error_contract_lookup", status=e.status)
            else:
                log.error("messari_contract_lookup_error", status=e.status)
            return None
        except Exception as e:
            log.error("messari_contract_lookup_exception", error=str(e))
            return None
