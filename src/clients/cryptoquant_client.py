"""
CryptoQuant API async client (Bitcoin on-chain analytics).

Provides BTC exchange flows, MVRV ratio, funding rates, and Coinbase premium index.
Used for macro regime detection in Phase 9.

Base URL: https://api.cryptoquant.com/v1
Auth: Authorization: Bearer <CRYPTOQUANT_API_KEY>
Rate limit: 1 req/sec (conservative — Pro tier can be strict)

Note: CryptoQuant Pro endpoints may return 403 on free plan keys.
All methods fail-safe: return {} on any error.
"""

from __future__ import annotations

import os
import time

from src.clients.base import BaseHTTPClient, HTTPError
from src.config import settings
from src.infra.cache import cached
from src.infra.logger import get_logger
from src.infra.rate_limiter import TokenBucket

log = get_logger(__name__)

_KEY_WARNING_EMITTED = False
# Dedup the 401/403 tier-forbidden warning per endpoint slug per process. The
# macro regime detector polls every few minutes — without dedup the same
# "Personal-tier doesn't include this endpoint" warning would log every cycle.
_TIER_FORBIDDEN_LOGGED: set[str] = set()


def _log_tier_forbidden_once(endpoint: str, status: int) -> None:
    """Log the tier-forbidden warning at most once per endpoint per process."""
    if endpoint in _TIER_FORBIDDEN_LOGGED:
        return
    _TIER_FORBIDDEN_LOGGED.add(endpoint)
    log.warning(
        "cryptoquant_auth_error",
        endpoint=endpoint,
        status=status,
        note=(
            "401/403 means either the API key is invalid OR this endpoint "
            "is not in your plan tier (Personal plan excludes most Pro "
            "on-chain metrics). Run client.discover_endpoints() or curl "
            "/v1/my/discovery/endpoints to list what's actually available."
        ),
    )


class CryptoQuantClient:
    """
    Async CryptoQuant client for BTC on-chain and market data.

    All methods return normalized dicts or {} on error (fail-safe).

    Usage::

        async with CryptoQuantClient() as client:
            flows = await client.get_btc_exchange_flows()
    """

    BASE_URL = "https://api.cryptoquant.com/v1"

    def __init__(self, api_key: str | None = None) -> None:
        global _KEY_WARNING_EMITTED  # noqa: PLW0603

        key = api_key or settings.cryptoquant_api_key or os.environ.get("CRYPTOQUANT_API_KEY", "")
        if not key and not _KEY_WARNING_EMITTED:
            log.warning(
                "cryptoquant_no_api_key",
                note="Running without API key — all CryptoQuant calls will fail gracefully. "
                     "Set CRYPTOQUANT_API_KEY for macro data.",
            )
            _KEY_WARNING_EMITTED = True

        headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": "solana-sniper-bot/0.1",
        }
        if key:
            headers["Authorization"] = f"Bearer {key}"

        self._http = BaseHTTPClient(
            base_url=self.BASE_URL,
            headers=headers,
            timeout=15.0,
            max_retries=2,
        )
        # 1 req/sec (conservative for CryptoQuant)
        self._limiter = TokenBucket(rps=1.0, burst=2.0, name="cryptoquant")

    async def close(self) -> None:
        await self._http.close()

    async def __aenter__(self) -> "CryptoQuantClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        await self._limiter.acquire()
        return await self._http.get(path, params=params)

    # ------------------------------------------------------------------
    # Plan tier discovery
    # ------------------------------------------------------------------

    async def discover_endpoints(self) -> list[str]:
        """
        Return the list of API paths this account's plan can access.

        CryptoQuant exposes `/v1/my/discovery/endpoints` (visible on the API
        Settings page at https://cryptoquant.com/account/api) which lists
        the endpoints the current API key is authorized to call. The
        Personal-use plan excludes most Pro on-chain metrics (exchange
        flows, MVRV, funding rates) — use this method to inspect what
        you can actually fetch before adjusting MacroRegime's expectations.

        Returns [] on error (caller should treat as "unknown — try everything").
        """
        try:
            result = await self._get("/my/discovery/endpoints")
        except HTTPError as e:
            log.warning("cryptoquant_discovery_http_error", status=e.status)
            return []
        except Exception as e:  # noqa: BLE001
            log.warning("cryptoquant_discovery_error", error=str(e))
            return []

        # Response can be flat {data: [...]} or wrapped {result: {data: [...]}}.
        data: object = None
        if isinstance(result, dict):
            if isinstance(result.get("data"), list):
                data = result["data"]
            else:
                inner = result.get("result")
                if isinstance(inner, dict) and isinstance(inner.get("data"), list):
                    data = inner["data"]
        data = data or []

        # Entries may be strings or dicts with `endpoint`/`path`/`url` key.
        out: list[str] = []
        if isinstance(data, list):
            for entry in data:
                if isinstance(entry, str):
                    out.append(entry)
                elif isinstance(entry, dict):
                    p = entry.get("endpoint") or entry.get("path") or entry.get("url")
                    if isinstance(p, str):
                        out.append(p)
        return out

    @staticmethod
    def _ms_ago(seconds: int) -> int:
        """Return unix timestamp in milliseconds, `seconds` ago."""
        return int((time.time() - seconds) * 1000)

    @staticmethod
    def _normalize(raw: dict) -> dict:
        """
        Normalize CryptoQuant response to consistent shape.

        CryptoQuant responses vary by endpoint but typically have
        {"result": {"data": [...]}} or {"data": [...]} structure.
        We normalize to {"data": [...], "status": "ok"}.
        """
        if not raw:
            return {}
        # Try common shapes
        if "result" in raw:
            inner = raw["result"]
            data = inner.get("data", inner) if isinstance(inner, dict) else inner
        elif "data" in raw:
            data = raw["data"]
        else:
            data = raw

        if not isinstance(data, list):
            data = [data] if data else []

        return {"data": data, "status": "ok"}

    # ------------------------------------------------------------------
    # BTC Exchange Flows
    # ------------------------------------------------------------------

    @cached(prefix="cryptoquant:exchange_flows", ttl=300)
    async def get_btc_exchange_flows(self) -> dict:
        """
        BTC exchange net flow (24h hourly window).

        Returns normalized {"data": [{...}], "status": "ok"} or {} on error.
        Endpoint: /btc/exchange-flows/netflow
        """
        try:
            result = await self._get(
                "/btc/exchange-flows/netflow",
                params={
                    "window": "hour",
                    "from": self._ms_ago(24 * 3600),
                },
            )
            return self._normalize(result)
        except HTTPError as e:
            if e.status in (401, 403):
                _log_tier_forbidden_once("exchange_flows", e.status)
            else:
                log.error("cryptoquant_exchange_flows_error", status=e.status, error=str(e))
            return {}
        except Exception as e:
            log.error("cryptoquant_exchange_flows_error", error=str(e))
            return {}

    # ------------------------------------------------------------------
    # BTC MVRV Ratio
    # ------------------------------------------------------------------

    @cached(prefix="cryptoquant:mvrv_ratio", ttl=300)
    async def get_btc_mvrv_ratio(self) -> dict:
        """
        BTC MVRV ratio (7-day daily window).

        MVRV > 3.7 historically signals market top; < 1 = undervalued.
        Returns normalized dict or {} on error.
        """
        try:
            result = await self._get(
                "/btc/network-data/mvrv-ratio",
                params={
                    "window": "day",
                    "from": self._ms_ago(7 * 24 * 3600),
                },
            )
            return self._normalize(result)
        except HTTPError as e:
            if e.status in (401, 403):
                _log_tier_forbidden_once("mvrv_ratio", e.status)
            else:
                log.error("cryptoquant_mvrv_ratio_error", status=e.status, error=str(e))
            return {}
        except Exception as e:
            log.error("cryptoquant_mvrv_ratio_error", error=str(e))
            return {}

    # ------------------------------------------------------------------
    # BTC Funding Rates
    # ------------------------------------------------------------------

    @cached(prefix="cryptoquant:funding_rates", ttl=300)
    async def get_btc_funding_rates(self) -> dict:
        """
        BTC perpetual funding rates (24h hourly window).

        High positive funding = crowded longs (potential squeeze risk).
        Returns normalized dict or {} on error.
        """
        try:
            result = await self._get(
                "/btc/market-data/funding-rates",
                params={
                    "window": "hour",
                    "from": self._ms_ago(24 * 3600),
                },
            )
            return self._normalize(result)
        except HTTPError as e:
            if e.status in (401, 403):
                _log_tier_forbidden_once("funding_rates", e.status)
            else:
                log.error("cryptoquant_funding_rates_error", status=e.status, error=str(e))
            return {}
        except Exception as e:
            log.error("cryptoquant_funding_rates_error", error=str(e))
            return {}

    # ------------------------------------------------------------------
    # BTC Coinbase Premium Index
    # ------------------------------------------------------------------

    @cached(prefix="cryptoquant:coinbase_premium", ttl=300)
    async def get_btc_coinbase_premium(self) -> dict:
        """
        BTC Coinbase premium index (24h hourly window).

        Positive premium = US institutional buying pressure (bullish).
        Negative premium = selling pressure or risk-off.
        Returns normalized dict or {} on error.
        """
        try:
            result = await self._get(
                "/btc/market-data/coinbase-premium-index",
                params={
                    "window": "hour",
                    "from": self._ms_ago(24 * 3600),
                },
            )
            return self._normalize(result)
        except HTTPError as e:
            if e.status in (401, 403):
                _log_tier_forbidden_once("coinbase_premium", e.status)
            else:
                log.error("cryptoquant_coinbase_premium_error", status=e.status, error=str(e))
            return {}
        except Exception as e:
            log.error("cryptoquant_coinbase_premium_error", error=str(e))
            return {}
