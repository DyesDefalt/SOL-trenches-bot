"""
CryptoPanic API async client.

CryptoPanic aggregates crypto news with community voting for bullish/bearish
sentiment. Public API requires auth_token as query parameter (NOT header).

Base URL: https://cryptopanic.com/api/v1
Auth: auth_token query param
Rate limit: 5 req/sec public — we use TokenBucket(rps=2.0, burst=5)

Docs: https://cryptopanic.com/developers/api/
"""

from __future__ import annotations

from collections import Counter

from src.clients.base import BaseHTTPClient, HTTPError
from src.config import settings
from src.infra.cache import cached
from src.infra.logger import get_logger
from src.infra.rate_limiter import TokenBucket

log = get_logger(__name__)

_INVALID_TOKEN = False  # set True on 401 so we stop hammering


def _normalize_post(post: dict) -> dict:
    """Normalize a raw CryptoPanic post to a consistent structure."""
    votes = post.get("votes") or {}
    positive = int(votes.get("positive", 0) or 0)
    negative = int(votes.get("negative", 0) or 0)
    important = int(votes.get("important", 0) or 0)
    sentiment_score = (positive - negative) / max(positive + negative, 1)

    source = post.get("source") or {}
    currencies = post.get("currencies") or []

    return {
        "title": post.get("title", ""),
        "url": post.get("url", ""),
        "source": source.get("title", "") if isinstance(source, dict) else str(source),
        "published_at": post.get("published_at", ""),
        "currencies": [c.get("code", "") for c in currencies if isinstance(c, dict)],
        "votes": {
            "positive": positive,
            "negative": negative,
            "important": important,
        },
        "sentiment_score": sentiment_score,
    }


class CryptoPanicClient:
    """
    Async CryptoPanic client.

    Usage::

        async with CryptoPanicClient() as client:
            posts = await client.get_solana_news(filter="hot")
    """

    BASE_URL = "https://cryptopanic.com/api/v1"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or settings.cryptopanic_api_key

        self._http = BaseHTTPClient(
            base_url=self.BASE_URL,
            headers={
                "Accept": "application/json",
                "User-Agent": "solana-sniper-bot/0.1",
            },
            timeout=15.0,
            max_retries=3,
        )
        # Conservative: 2 req/sec, burst of 5 (public limit is 5 req/sec)
        self._limiter = TokenBucket(rps=2.0, burst=5, name="cryptopanic")

    async def close(self) -> None:
        await self._http.close()

    async def __aenter__(self) -> "CryptoPanicClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    def _base_params(self) -> dict:
        """Base query params including auth."""
        return {"auth_token": self._api_key, "public": "true"}

    async def _get(self, path: str, params: dict | None = None) -> dict:
        await self._limiter.acquire()
        return await self._http.get(path, params=params)

    # ------------------------------------------------------------------
    # Solana-wide news
    # ------------------------------------------------------------------

    @cached(prefix="cryptopanic:sol_news", ttl=60)
    async def get_solana_news(self, filter: str = "hot", limit: int = 20) -> list[dict]:
        """
        Fetch recent Solana news posts.

        filter options: "hot", "rising", "bullish", "bearish", "important", "saved"
        Returns normalized list of posts or [] on error.
        """
        global _INVALID_TOKEN  # noqa: PLW0603
        if _INVALID_TOKEN:
            return []

        params = {
            **self._base_params(),
            "currencies": "SOL",
            "filter": filter,
            "kind": "news",
            "regions": "en",
        }
        try:
            result = await self._get("/posts/", params=params)
            results = result.get("results", [])
            return [_normalize_post(p) for p in results[:limit]]
        except HTTPError as e:
            if e.status == 401:
                log.error(
                    "cryptopanic_invalid_token",
                    note="Auth token rejected — disabling for this session.",
                )
                _INVALID_TOKEN = True
            else:
                log.error("cryptopanic_sol_news_error", status=e.status, filter=filter)
            return []
        except Exception as e:
            log.error("cryptopanic_sol_news_exception", error=str(e))
            return []

    # ------------------------------------------------------------------
    # Per-token news
    # ------------------------------------------------------------------

    @cached(prefix="cryptopanic:token_news", ttl=60)
    async def get_token_news(self, ticker: str, filter: str = "hot") -> list[dict]:
        """
        Fetch news for a specific token ticker (e.g. "BONK", "WIF").

        Returns normalized list of posts or [] on error.
        """
        global _INVALID_TOKEN  # noqa: PLW0603
        if _INVALID_TOKEN:
            return []

        params = {
            **self._base_params(),
            "currencies": ticker.upper(),
            "filter": filter,
        }
        try:
            result = await self._get("/posts/", params=params)
            results = result.get("results", [])
            return [_normalize_post(p) for p in results]
        except HTTPError as e:
            if e.status == 401:
                log.error(
                    "cryptopanic_invalid_token",
                    note="Auth token rejected — disabling for this session.",
                )
                _INVALID_TOKEN = True
            else:
                log.error("cryptopanic_token_news_error", ticker=ticker, status=e.status)
            return []
        except Exception as e:
            log.error("cryptopanic_token_news_exception", ticker=ticker, error=str(e))
            return []

    # ------------------------------------------------------------------
    # Trending currencies aggregated from hot posts
    # ------------------------------------------------------------------

    @cached(prefix="cryptopanic:trending", ttl=60)
    async def get_trending_currencies(self) -> list[str]:
        """
        Aggregate ticker mentions from hot posts to find trending currencies.

        Returns list of ticker symbols sorted by mention count (most mentioned first).
        """
        posts = await self.get_solana_news(filter="hot", limit=50)
        if not posts:
            # Fall back to fetching without filter for broader data
            try:
                params = {
                    **self._base_params(),
                    "filter": "hot",
                    "kind": "news",
                    "regions": "en",
                }
                result = await self._get("/posts/", params=params)
                raw_results = result.get("results", [])
                posts = [_normalize_post(p) for p in raw_results[:50]]
            except Exception as e:
                log.error("cryptopanic_trending_fallback_error", error=str(e))
                return []

        counter: Counter[str] = Counter()
        for post in posts:
            for currency in post.get("currencies", []):
                if currency:
                    counter[currency] += 1

        return [ticker for ticker, _ in counter.most_common()]
