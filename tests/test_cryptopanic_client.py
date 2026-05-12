"""
Tests for CryptoPanicClient.

Coverage:
- get_solana_news: normal response, empty results
- get_token_news: normal response
- get_trending_currencies: aggregation from posts
- 401 handling: sets _INVALID_TOKEN, returns [] permanently
- sentiment_score calculation
- _normalize_post edge cases (missing fields)
"""

from __future__ import annotations

import sys
import types

# ---------------------------------------------------------------------------
# Stub out infra modules before importing clients (Python 3.9 compat)
# ---------------------------------------------------------------------------

def _make_cached_stub():
    """Return a no-op @cached decorator."""
    def cached(prefix: str, ttl: int, skip_cache=None):
        def decorator(func):
            return func
        return decorator
    return cached


def _stub_infra_modules():
    # src.infra.cache
    if "src.infra.cache" not in sys.modules:
        m = types.ModuleType("src.infra.cache")
        m.cached = _make_cached_stub()
        class _Cache:
            async def get(self, key): return None
            async def set(self, key, value, ttl): pass
        m.cache = _Cache()
        sys.modules["src.infra.cache"] = m

    # src.infra.rate_limiter — real module, but needs asyncio; provide stub if missing
    if "src.infra.rate_limiter" not in sys.modules:
        m = types.ModuleType("src.infra.rate_limiter")
        class TokenBucket:
            def __init__(self, rps=1.0, burst=None, name=""):
                pass
            async def acquire(self):
                pass
        m.TokenBucket = TokenBucket
        sys.modules["src.infra.rate_limiter"] = m

    # src.infra.logger
    if "src.infra.logger" not in sys.modules:
        m = types.ModuleType("src.infra.logger")

        class _KwLogger:
            def __init__(self, name: str):
                self.name = name
            def _noop(self, msg, *args, **kwargs): pass
            debug = info = warning = error = critical = _noop

        m.get_logger = lambda name: _KwLogger(name)
        sys.modules["src.infra.logger"] = m


_stub_infra_modules()

# ---------------------------------------------------------------------------
# Now safe to import
# ---------------------------------------------------------------------------

from unittest.mock import AsyncMock, patch  # noqa: E402

import pytest  # noqa: E402

import src.clients.cryptopanic_client as cp_module  # noqa: E402
from src.clients.cryptopanic_client import CryptoPanicClient, _normalize_post  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_raw_post(
    title: str = "Test Title",
    url: str = "https://example.com",
    source_title: str = "CoinDesk",
    published_at: str = "2026-05-12T10:00:00Z",
    currencies: list | None = None,
    positive: int = 10,
    negative: int = 2,
    important: int = 3,
) -> dict:
    return {
        "title": title,
        "url": url,
        "source": {"title": source_title},
        "published_at": published_at,
        "currencies": currencies or [{"code": "SOL"}],
        "votes": {"positive": positive, "negative": negative, "important": important},
    }


def _mock_http_response(results: list) -> dict:
    return {"results": results}


# ---------------------------------------------------------------------------
# Unit: _normalize_post
# ---------------------------------------------------------------------------

class TestNormalizePost:
    def test_sentiment_score_positive(self):
        """positive >> negative → positive sentiment_score."""
        post = _make_raw_post(positive=10, negative=0)
        normalized = _normalize_post(post)
        assert normalized["sentiment_score"] == pytest.approx(1.0)

    def test_sentiment_score_negative(self):
        """negative >> positive → negative sentiment_score."""
        post = _make_raw_post(positive=0, negative=10)
        normalized = _normalize_post(post)
        assert normalized["sentiment_score"] == pytest.approx(-1.0)

    def test_sentiment_score_balanced(self):
        """Equal votes → score 0.0."""
        post = _make_raw_post(positive=5, negative=5)
        normalized = _normalize_post(post)
        assert normalized["sentiment_score"] == pytest.approx(0.0)

    def test_sentiment_score_zero_votes(self):
        """No votes → score 0, no division by zero."""
        post = _make_raw_post(positive=0, negative=0)
        normalized = _normalize_post(post)
        assert normalized["sentiment_score"] == pytest.approx(0.0)

    def test_currencies_extracted(self):
        post = _make_raw_post(currencies=[{"code": "SOL"}, {"code": "BONK"}])
        normalized = _normalize_post(post)
        assert "SOL" in normalized["currencies"]
        assert "BONK" in normalized["currencies"]

    def test_missing_fields_handled_gracefully(self):
        """Empty dict should not raise."""
        normalized = _normalize_post({})
        assert normalized["title"] == ""
        assert normalized["sentiment_score"] == pytest.approx(0.0)
        assert normalized["currencies"] == []


# ---------------------------------------------------------------------------
# Integration: CryptoPanicClient with mocked HTTP
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_invalid_token():
    """Reset global _INVALID_TOKEN between tests."""
    cp_module._INVALID_TOKEN = False
    yield
    cp_module._INVALID_TOKEN = False


@pytest.fixture
def client():
    """Create CryptoPanicClient with mocked BaseHTTPClient (no h2 dependency)."""
    with patch("src.clients.cryptopanic_client.BaseHTTPClient") as mock_http_cls:
        mock_http_cls.return_value = AsyncMock()
        c = CryptoPanicClient(api_key="test_key")
    return c


class TestGetSolanaNews:
    @pytest.mark.asyncio
    async def test_returns_normalized_posts(self, client):
        raw = [_make_raw_post(title="SOL Pumps Hard"), _make_raw_post(title="Solana TVL ATH")]
        with patch.object(client, "_get", new=AsyncMock(return_value=_mock_http_response(raw))):
            posts = await client.get_solana_news()
        assert len(posts) == 2
        assert posts[0]["title"] == "SOL Pumps Hard"
        assert "sentiment_score" in posts[0]

    @pytest.mark.asyncio
    async def test_empty_results_returns_empty_list(self, client):
        with patch.object(client, "_get", new=AsyncMock(return_value={"results": []})):
            posts = await client.get_solana_news()
        assert posts == []

    @pytest.mark.asyncio
    async def test_401_sets_invalid_token_and_returns_empty(self, client):
        from src.clients.base import HTTPError
        with patch.object(
            client, "_get",
            new=AsyncMock(side_effect=HTTPError("Unauthorized", status=401)),
        ):
            posts = await client.get_solana_news()
        assert posts == []
        assert cp_module._INVALID_TOKEN is True

    @pytest.mark.asyncio
    async def test_subsequent_calls_skipped_after_401(self, client):
        cp_module._INVALID_TOKEN = True
        mock_get = AsyncMock()
        with patch.object(client, "_get", new=mock_get):
            posts = await client.get_solana_news()
        assert posts == []
        mock_get.assert_not_called()

    @pytest.mark.asyncio
    async def test_network_exception_returns_empty(self, client):
        with patch.object(client, "_get", new=AsyncMock(side_effect=Exception("timeout"))):
            posts = await client.get_solana_news()
        assert posts == []


class TestGetTokenNews:
    @pytest.mark.asyncio
    async def test_returns_posts_for_ticker(self, client):
        raw = [_make_raw_post(title="BONK Surges", currencies=[{"code": "BONK"}])]
        with patch.object(client, "_get", new=AsyncMock(return_value=_mock_http_response(raw))):
            posts = await client.get_token_news("BONK")
        assert len(posts) == 1
        assert posts[0]["title"] == "BONK Surges"


class TestGetTrendingCurrencies:
    @pytest.mark.asyncio
    async def test_aggregates_ticker_mentions(self, client):
        normalized_posts = [
            {**_normalize_post(_make_raw_post(currencies=[{"code": "SOL"}, {"code": "BONK"}]))},
            {**_normalize_post(_make_raw_post(currencies=[{"code": "SOL"}]))},
            {**_normalize_post(_make_raw_post(currencies=[{"code": "WIF"}, {"code": "BONK"}]))},
        ]
        with patch.object(client, "get_solana_news", new=AsyncMock(return_value=normalized_posts)):
            trending = await client.get_trending_currencies()
        # SOL: 2, BONK: 2, WIF: 1
        assert "SOL" in trending
        assert "BONK" in trending
        # WIF should appear after higher-count tickers
        assert trending.index("WIF") >= trending.index("SOL") or trending.index("WIF") >= trending.index("BONK")

    @pytest.mark.asyncio
    async def test_empty_posts_returns_list(self, client):
        with patch.object(client, "get_solana_news", new=AsyncMock(return_value=[])):
            with patch.object(client, "_get", new=AsyncMock(return_value={"results": []})):
                trending = await client.get_trending_currencies()
        assert isinstance(trending, list)
