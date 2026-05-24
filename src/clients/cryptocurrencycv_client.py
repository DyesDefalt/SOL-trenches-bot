"""
cryptocurrency.cv (Free Crypto News API) — drop-in CryptoPanic replacement.

Open-source, no API key, no signup, no rate-limit-on-paper. Mirrors the
CryptoPanic client interface so callers (news_aggregator, phase9_smoke,
etc.) can switch backends with a one-line import change.

Endpoints used (per https://github.com/nirholas/cryptocurrency.cv):
- GET /api/news?category=<cat>&limit=<n>     → latest news
- GET /api/search?q=<ticker>&limit=<n>       → token-specific news
- GET /api/trending?hours=24                 → trending tickers + sentiment
- GET /api/sentiment?asset=<asset>           → asset sentiment (bonus, not yet wired)
- GET /api/analyze?sentiment=<polarity>      → filter by sentiment

Why we migrated off CryptoPanic:
- CryptoPanic v1 was retired (returns 404 HTML).
- CryptoPanic v2 forces a paid plan after April 1, 2026.
- cryptocurrency.cv covers the same use case (Solana news + sentiment +
  trending tickers) for free and aggregates 130+ sources.

Article schema returned by cryptocurrency.cv:
    {
      id, title, link, description, pubDate (ISO 8601), source, sourceKey,
      category, timeAgo, sentiment ("bullish"|"bearish"|"neutral"|null),
      tags: [...]
    }

We normalize to the SAME shape as CryptoPanic's _normalize_post so callers
need zero schema changes:
    {
      title, url, source, published_at, currencies: [str],
      votes: {positive, negative, important},
      sentiment_score: float in [-1.0, 1.0]
    }

Docs: https://cryptocurrency.cv/api/llms-full.txt
"""

from __future__ import annotations

from collections import Counter

from src.clients.base import BaseHTTPClient, HTTPError
from src.config import settings
from src.infra.cache import cached
from src.infra.logger import get_logger
from src.infra.rate_limiter import TokenBucket

log = get_logger(__name__)


# Map cryptocurrency.cv `sentiment` field → numeric score expected by callers
# that previously processed CryptoPanic's votes-derived sentiment_score.
_SENTIMENT_TO_SCORE = {
    "bullish": 1.0,
    "positive": 1.0,
    "bearish": -1.0,
    "negative": -1.0,
    "neutral": 0.0,
}


def _extract_currencies(article: dict) -> list[str]:
    """Extract ticker symbols mentioned in an article.

    cryptocurrency.cv exposes them in `tags` (mixed lowercase) and sometimes
    in dedicated `tickers` field. We uppercase + dedupe + drop non-ticker
    tags (categories like "defi", "regulation", common words).
    """
    candidates: list[str] = []
    for source_key in ("tickers", "tags"):
        items = article.get(source_key) or []
        if isinstance(items, list):
            candidates.extend(str(x) for x in items if x)

    # Heuristic: real ticker symbols are 2-6 uppercase alphanumerics. Tags like
    # "defi" / "regulation" / "ath" are lowercase descriptive labels — drop.
    non_tickers = {
        "defi", "nft", "regulation", "general", "altcoin", "market", "price",
        "news", "ath", "atl", "bullish", "bearish", "neutral", "hack",
        "exploit", "scam", "rug", "pump", "dump", "trade", "trading",
    }
    seen: set[str] = set()
    out: list[str] = []
    for raw in candidates:
        t = raw.strip().upper()
        if not t or t.lower() in non_tickers:
            continue
        if not (2 <= len(t) <= 8):
            continue
        if not t.replace(".", "").replace("-", "").isalnum():
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _normalize_article(article: dict) -> dict:
    """Map a cryptocurrency.cv article into the CryptoPanic _normalize_post shape."""
    sentiment_raw = (article.get("sentiment") or "").strip().lower()
    sentiment_score = _SENTIMENT_TO_SCORE.get(sentiment_raw, 0.0)
    return {
        "title": article.get("title", ""),
        "url": article.get("link") or article.get("url", ""),
        "source": article.get("source") or article.get("sourceKey", ""),
        "published_at": article.get("pubDate") or article.get("published_at", ""),
        "currencies": _extract_currencies(article),
        # cryptocurrency.cv doesn't expose community voting — synthesize from
        # sentiment polarity so downstream sentiment math still works.
        "votes": {
            "positive": 1 if sentiment_score > 0 else 0,
            "negative": 1 if sentiment_score < 0 else 0,
            "important": 0,
        },
        "sentiment_score": sentiment_score,
    }


class CryptoCurrencyCvClient:
    """
    Async client for cryptocurrency.cv (Free Crypto News API).

    No API key required — instantiate and use:

        async with CryptoCurrencyCvClient() as client:
            posts = await client.get_solana_news(filter="hot")
            sentiment_score = await client.get_asset_sentiment("SOL")
    """

    _DEFAULT_BASE_URL = "https://cryptocurrency.cv"

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = (
            base_url
            or getattr(settings, "cryptocurrencycv_base_url", None)
            or self._DEFAULT_BASE_URL
        ).rstrip("/")

        self._http = BaseHTTPClient(
            base_url=self._base_url,
            headers={
                "Accept": "application/json",
                "User-Agent": "solana-sniper-bot/0.1",
            },
            timeout=15.0,
            max_retries=3,
        )
        # No published rate limit; be polite (2 RPS sustained, burst of 5).
        self._limiter = TokenBucket(rps=2.0, burst=5, name="cryptocurrencycv")

    async def close(self) -> None:
        await self._http.close()

    async def __aenter__(self) -> "CryptoCurrencyCvClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        await self._limiter.acquire()
        return await self._http.get(path, params=params)

    # ------------------------------------------------------------------
    # CryptoPanic-compatible interface (news_aggregator + phase9_smoke
    # call these signatures unchanged)
    # ------------------------------------------------------------------

    @cached(prefix="ccv:sol_news", ttl=60)
    async def get_solana_news(self, filter: str = "hot", limit: int = 20) -> list[dict]:
        """
        Fetch Solana news, optionally filtered by sentiment.

        `filter` accepts the same values as the CryptoPanic client:
        - "hot" / "rising"             → newest articles in solana category
        - "bullish" / "important"      → articles where sentiment == bullish
        - "bearish"                    → articles where sentiment == bearish

        Returns a list normalized to the CryptoPanic post shape (see
        `_normalize_article` above).
        """
        try:
            # /api/news supports category filtering. We pull a slightly larger
            # page than `limit` so the local sentiment filter doesn't starve.
            fetch_limit = min(max(limit * 3, limit), 100)
            result = await self._get(
                "/api/news",
                params={"category": "solana", "limit": fetch_limit},
            )
            articles = result.get("articles") or []
            if not isinstance(articles, list):
                return []

            normalized = [_normalize_article(a) for a in articles]

            f = (filter or "").strip().lower()
            if f in ("bullish", "important"):
                filtered = [p for p in normalized if p["sentiment_score"] > 0]
            elif f == "bearish":
                filtered = [p for p in normalized if p["sentiment_score"] < 0]
            else:  # "hot", "rising", anything else → all
                filtered = normalized

            return filtered[:limit]
        except HTTPError as e:
            log.error(
                "ccv_sol_news_error",
                status=e.status,
                filter=filter,
                body=(e.body[:200] if e.body else None),
            )
            return []
        except Exception as e:
            log.error("ccv_sol_news_exception", error=str(e))
            return []

    @cached(prefix="ccv:token_news", ttl=60)
    async def get_token_news(self, ticker: str, filter: str = "hot") -> list[dict]:
        """
        Fetch news mentioning a specific token ticker (e.g. "BONK", "WIF").

        Uses /api/search?q=<ticker>; the response shape matches /api/news.
        Optional sentiment filter applied locally.
        """
        try:
            ticker_clean = (ticker or "").strip()
            if not ticker_clean:
                return []
            result = await self._get(
                "/api/search",
                params={"q": ticker_clean, "limit": 20},
            )
            articles = result.get("articles") or result.get("results") or []
            if not isinstance(articles, list):
                return []
            normalized = [_normalize_article(a) for a in articles]

            f = (filter or "").strip().lower()
            if f in ("bullish", "important"):
                normalized = [p for p in normalized if p["sentiment_score"] > 0]
            elif f == "bearish":
                normalized = [p for p in normalized if p["sentiment_score"] < 0]
            return normalized
        except HTTPError as e:
            log.error(
                "ccv_token_news_error",
                ticker=ticker,
                status=e.status,
            )
            return []
        except Exception as e:
            log.error("ccv_token_news_exception", ticker=ticker, error=str(e))
            return []

    @cached(prefix="ccv:trending", ttl=60)
    async def get_trending_currencies(self) -> list[str]:
        """
        Return tickers with mention activity in the last 24h, most-mentioned first.

        Prefer the dedicated /api/trending endpoint (returns mention counts +
        sentiment). Fall back to aggregating tags from hot news if that fails.
        """
        try:
            result = await self._get("/api/trending", params={"hours": 24})
            topics = (
                result.get("trending")
                or result.get("topics")
                or result.get("data")
                or []
            )
            if isinstance(topics, list) and topics:
                counter: Counter[str] = Counter()
                for entry in topics:
                    if not isinstance(entry, dict):
                        continue
                    # Endpoint can return tickers under various keys.
                    raw = (
                        entry.get("ticker")
                        or entry.get("symbol")
                        or entry.get("name")
                        or entry.get("topic")
                        or ""
                    )
                    count = int(entry.get("count", 0) or 0)
                    if raw and isinstance(raw, str):
                        counter[raw.upper()] += max(count, 1)
                if counter:
                    return [t for t, _ in counter.most_common()]
        except HTTPError as e:
            log.warning(
                "ccv_trending_endpoint_error",
                status=e.status,
                note="Falling back to news-tag aggregation.",
            )
        except Exception as e:
            log.debug(
                "ccv_trending_endpoint_exception",
                error=str(e),
                note="Falling back to news-tag aggregation.",
            )

        # Fallback: aggregate from /api/news ticker tags.
        try:
            posts = await self.get_solana_news(filter="hot", limit=50)
            counter = Counter()
            for post in posts:
                for ticker in post.get("currencies", []):
                    if ticker:
                        counter[ticker] += 1
            return [ticker for ticker, _ in counter.most_common()]
        except Exception as e:
            log.error("ccv_trending_fallback_error", error=str(e))
            return []

    # ------------------------------------------------------------------
    # Bonus: direct asset sentiment (not on CryptoPanic; available here)
    # ------------------------------------------------------------------

    @cached(prefix="ccv:asset_sentiment", ttl=120)
    async def get_asset_sentiment(self, asset: str, period: str = "24h") -> dict:
        """
        Return aggregated AI sentiment for an asset.

        Response shape from cryptocurrency.cv:
            {
              "overall": "bullish",
              "score": 0.72,                       // -1.0 .. 1.0
              "breakdown": {"bullish": 65, "neutral": 20, "bearish": 15},
              "articleCount": ...,
            }

        `period`: "1h" | "24h" | "7d" | "30d". Returns {} on error.
        """
        try:
            result = await self._get(
                "/api/sentiment",
                params={"asset": asset.upper(), "period": period},
            )
            # Some deployments wrap by asset code; normalize to flat dict.
            if isinstance(result, dict) and asset.upper() in (result.get("assets") or {}):
                return result["assets"][asset.upper()]
            return result if isinstance(result, dict) else {}
        except HTTPError as e:
            log.error("ccv_asset_sentiment_error", asset=asset, status=e.status)
            return {}
        except Exception as e:
            log.error("ccv_asset_sentiment_exception", asset=asset, error=str(e))
            return {}
