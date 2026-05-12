"""
Alpha Vantage API async client (equity + macro market data).

Provides SPX (via SPY ETF), DXY (via UUP ETF), VIX (via VIXY), and BTC daily candles.
Used for macro regime detection in Phase 9.

Base URL: https://www.alphavantage.co
Auth: apikey query parameter
Rate limit: 5 req/min (free tier) — TokenBucket(rps=0.08, burst=1)
Cache TTL: 900s (15 min) — aggressive caching due to tight rate limits

Note: Alpha Vantage free tier is very strict on rate. Cache is critical.
All methods fail-safe: return {} on any error.
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


class AlphaVantageClient:
    """
    Async Alpha Vantage client for equity and macro market quotes.

    All methods return {"price": float, "change_pct": float, "raw": {...}} or {} on error.

    Usage::

        async with AlphaVantageClient() as client:
            spx = await client.get_spx_quote()
            dxy = await client.get_dxy_quote()
    """

    BASE_URL = "https://www.alphavantage.co"

    def __init__(self, api_key: str | None = None) -> None:
        global _KEY_WARNING_EMITTED  # noqa: PLW0603

        self._api_key = (
            api_key
            or settings.alphavantage_api_key
            or os.environ.get("ALPHAVANTAGE_API_KEY", "")
        )
        if not self._api_key and not _KEY_WARNING_EMITTED:
            log.warning(
                "alphavantage_no_api_key",
                note="Running without API key — all Alpha Vantage calls will fail gracefully. "
                     "Set ALPHAVANTAGE_API_KEY for macro data.",
            )
            _KEY_WARNING_EMITTED = True

        self._http = BaseHTTPClient(
            base_url=self.BASE_URL,
            headers={
                "Accept": "application/json",
                "User-Agent": "solana-sniper-bot/0.1",
            },
            timeout=20.0,
            max_retries=2,
        )
        # 5 req/min free tier → ~0.083 rps. Use 0.08 to be safe with burst=1.
        self._limiter = TokenBucket(rps=0.08, burst=1.0, name="alphavantage")

    async def close(self) -> None:
        await self._http.close()

    async def __aenter__(self) -> "AlphaVantageClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def _get(self, params: dict) -> dict:
        """Add API key to params and acquire rate limit before requesting."""
        await self._limiter.acquire()
        params_with_key = {**params, "apikey": self._api_key}
        return await self._http.get("/query", params=params_with_key)

    @staticmethod
    def _parse_global_quote(raw: dict) -> dict:
        """
        Parse Alpha Vantage 'Global Quote' response shape.

        Input raw response has "Global Quote" key with fields like:
          "05. price", "09. change", "10. change percent"

        Returns {"price": float, "change_pct": float, "raw": {...}} or {} if malformed.
        """
        gq = raw.get("Global Quote", {})
        if not gq:
            # Alpha Vantage returns {"Note": "..."} on rate limit, or empty on bad symbol
            note = raw.get("Note") or raw.get("Information") or ""
            if note:
                log.warning("alphavantage_api_note", note=note[:200])
            return {}

        try:
            price_str = gq.get("05. price", "0") or "0"
            change_pct_str = gq.get("10. change percent", "0%") or "0%"

            # Strip trailing "%" before parsing
            change_pct_clean = change_pct_str.replace("%", "").strip()

            price = float(price_str)
            change_pct = float(change_pct_clean)

            return {"price": price, "change_pct": change_pct, "raw": gq}
        except (ValueError, TypeError) as e:
            log.warning("alphavantage_parse_error", error=str(e), raw=str(gq)[:200])
            return {}

    # ------------------------------------------------------------------
    # SPX Quote (via SPY ETF)
    # ------------------------------------------------------------------

    @cached(prefix="alphavantage:spx", ttl=900)
    async def get_spx_quote(self) -> dict:
        """
        S&P 500 proxy quote via SPY ETF.

        Returns {"price": float, "change_pct": float, "raw": {...}} or {} on error.
        """
        try:
            result = await self._get(
                {"function": "GLOBAL_QUOTE", "symbol": "SPY"}
            )
            return self._parse_global_quote(result)
        except HTTPError as e:
            log.error("alphavantage_spx_error", status=e.status, error=str(e))
            return {}
        except Exception as e:
            log.error("alphavantage_spx_error", error=str(e))
            return {}

    # ------------------------------------------------------------------
    # DXY Quote (via UUP ETF)
    # ------------------------------------------------------------------

    @cached(prefix="alphavantage:dxy", ttl=900)
    async def get_dxy_quote(self) -> dict:
        """
        US Dollar Index proxy quote via UUP (Invesco DB USD Index Bullish Fund ETF).

        Returns {"price": float, "change_pct": float, "raw": {...}} or {} on error.
        """
        try:
            result = await self._get(
                {"function": "GLOBAL_QUOTE", "symbol": "UUP"}
            )
            return self._parse_global_quote(result)
        except HTTPError as e:
            log.error("alphavantage_dxy_error", status=e.status, error=str(e))
            return {}
        except Exception as e:
            log.error("alphavantage_dxy_error", error=str(e))
            return {}

    # ------------------------------------------------------------------
    # VIX Quote (via VIXY ETF)
    # ------------------------------------------------------------------

    @cached(prefix="alphavantage:vix", ttl=900)
    async def get_vix_quote(self) -> dict:
        """
        VIX (volatility index) proxy quote via VIXY (ProShares VIX Short-Term Futures ETF).

        VIXY > 30 equivalent signals extreme fear.
        Returns {"price": float, "change_pct": float, "raw": {...}} or {} on error.
        """
        try:
            result = await self._get(
                {"function": "GLOBAL_QUOTE", "symbol": "VIXY"}
            )
            return self._parse_global_quote(result)
        except HTTPError as e:
            log.error("alphavantage_vix_error", status=e.status, error=str(e))
            return {}
        except Exception as e:
            log.error("alphavantage_vix_error", error=str(e))
            return {}

    # ------------------------------------------------------------------
    # BTC Daily Candles
    # ------------------------------------------------------------------

    @cached(prefix="alphavantage:btc_daily", ttl=900)
    async def get_btc_daily(self) -> dict:
        """
        BTC/USD daily candles from Alpha Vantage digital currency endpoint.

        Returns raw response dict with "Time Series (Digital Currency Daily)" key,
        or {} on error. Caller extracts latest close for 24h change calculation.
        """
        try:
            result = await self._get(
                {
                    "function": "DIGITAL_CURRENCY_DAILY",
                    "symbol": "BTC",
                    "market": "USD",
                }
            )
            # Check for API limit note
            note = result.get("Note") or result.get("Information") or ""
            if note:
                log.warning("alphavantage_api_note", note=note[:200])
                return {}

            ts_key = "Time Series (Digital Currency Daily)"
            if ts_key not in result:
                log.warning("alphavantage_btc_daily_missing_key", keys=list(result.keys())[:5])
                return {}

            return result
        except HTTPError as e:
            log.error("alphavantage_btc_daily_error", status=e.status, error=str(e))
            return {}
        except Exception as e:
            log.error("alphavantage_btc_daily_error", error=str(e))
            return {}
