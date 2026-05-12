"""
Tests for MessariClient.

Coverage:
- get_asset_profile: success, 404 (missing asset), 403 (rate limit)
- get_asset_metrics: success, 404
- get_news: success, auth error
- get_asset_news: success, 404
- find_slug_by_contract: found, not found, exception
"""

from __future__ import annotations

import sys
import types


# ---------------------------------------------------------------------------
# Stub out infra modules before importing clients (Python 3.9 compat)
# ---------------------------------------------------------------------------

def _stub_infra_modules():
    if "src.infra.cache" not in sys.modules:
        m = types.ModuleType("src.infra.cache")
        def cached(prefix: str, ttl: int, skip_cache=None):
            def decorator(func):
                return func
            return decorator
        m.cached = cached
        sys.modules["src.infra.cache"] = m

    if "src.infra.rate_limiter" not in sys.modules:
        m = types.ModuleType("src.infra.rate_limiter")
        class TokenBucket:
            def __init__(self, rps=1.0, burst=None, name=""):
                pass
            async def acquire(self):
                pass
        m.TokenBucket = TokenBucket
        sys.modules["src.infra.rate_limiter"] = m

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

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

from src.clients.messari_client import MessariClient  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_profile_response(slug: str = "solana") -> dict:
    return {
        "data": {
            "id": f"id-{slug}",
            "slug": slug,
            "name": slug.capitalize(),
            "profile": {
                "general": {
                    "overview": {"tagline": "Fast blockchain"},
                    "category": "Layer 1",
                },
                "contract_addresses": [
                    {"chain": "solana", "contract_address": "So111"},
                ],
            },
        }
    }


def _make_metrics_response(slug: str = "solana") -> dict:
    return {
        "data": {
            "id": f"id-{slug}",
            "slug": slug,
            "market_data": {"price_usd": 150.0, "market_cap": 70_000_000_000.0},
        }
    }


def _make_news_list(count: int = 3) -> dict:
    return {
        "data": [
            {"id": str(i), "title": f"News {i}", "published_at": "2026-05-12T10:00:00Z"}
            for i in range(count)
        ]
    }


@pytest.fixture
def client():
    """Create MessariClient with mocked BaseHTTPClient (no h2 dependency)."""
    with patch("src.clients.messari_client.BaseHTTPClient") as mock_http_cls:
        mock_http_cls.return_value = AsyncMock()
        c = MessariClient(api_key="test_key_123")
    return c


# ---------------------------------------------------------------------------
# get_asset_profile
# ---------------------------------------------------------------------------

class TestGetAssetProfile:
    @pytest.mark.asyncio
    async def test_returns_profile_data(self, client):
        with patch.object(client, "_get", new=AsyncMock(return_value=_make_profile_response("solana"))):
            profile = await client.get_asset_profile("solana")
        assert profile["slug"] == "solana"

    @pytest.mark.asyncio
    async def test_404_returns_empty_dict(self, client):
        from src.clients.base import HTTPError
        with patch.object(
            client, "_get",
            new=AsyncMock(side_effect=HTTPError("Not Found", status=404)),
        ):
            profile = await client.get_asset_profile("unknown-memecoin-xyz")
        assert profile == {}

    @pytest.mark.asyncio
    async def test_403_returns_empty_dict(self, client):
        from src.clients.base import HTTPError
        with patch.object(
            client, "_get",
            new=AsyncMock(side_effect=HTTPError("Forbidden", status=403)),
        ):
            profile = await client.get_asset_profile("solana")
        assert profile == {}

    @pytest.mark.asyncio
    async def test_exception_returns_empty_dict(self, client):
        with patch.object(client, "_get", new=AsyncMock(side_effect=Exception("timeout"))):
            profile = await client.get_asset_profile("solana")
        assert profile == {}


# ---------------------------------------------------------------------------
# get_asset_metrics
# ---------------------------------------------------------------------------

class TestGetAssetMetrics:
    @pytest.mark.asyncio
    async def test_returns_metrics_data(self, client):
        with patch.object(client, "_get", new=AsyncMock(return_value=_make_metrics_response("solana"))):
            metrics = await client.get_asset_metrics("solana")
        assert "market_data" in metrics

    @pytest.mark.asyncio
    async def test_404_returns_empty_dict(self, client):
        from src.clients.base import HTTPError
        with patch.object(
            client, "_get",
            new=AsyncMock(side_effect=HTTPError("Not Found", status=404)),
        ):
            metrics = await client.get_asset_metrics("new-memecoin-xyz")
        assert metrics == {}


# ---------------------------------------------------------------------------
# get_news
# ---------------------------------------------------------------------------

class TestGetNews:
    @pytest.mark.asyncio
    async def test_returns_list_of_news(self, client):
        with patch.object(client, "_get", new=AsyncMock(return_value=_make_news_list(5))):
            news = await client.get_news(limit=5)
        assert len(news) == 5
        assert news[0]["title"] == "News 0"

    @pytest.mark.asyncio
    async def test_limit_respected(self, client):
        with patch.object(client, "_get", new=AsyncMock(return_value=_make_news_list(20))):
            news = await client.get_news(limit=3)
        assert len(news) == 3

    @pytest.mark.asyncio
    async def test_401_returns_empty_list(self, client):
        from src.clients.base import HTTPError
        with patch.object(
            client, "_get",
            new=AsyncMock(side_effect=HTTPError("Unauthorized", status=401)),
        ):
            news = await client.get_news()
        assert news == []


# ---------------------------------------------------------------------------
# get_asset_news
# ---------------------------------------------------------------------------

class TestGetAssetNews:
    @pytest.mark.asyncio
    async def test_returns_asset_news(self, client):
        with patch.object(client, "_get", new=AsyncMock(return_value=_make_news_list(3))):
            news = await client.get_asset_news("bonk")
        assert len(news) == 3

    @pytest.mark.asyncio
    async def test_404_returns_empty_list(self, client):
        from src.clients.base import HTTPError
        with patch.object(
            client, "_get",
            new=AsyncMock(side_effect=HTTPError("Not Found", status=404)),
        ):
            news = await client.get_asset_news("unknown-xyz")
        assert news == []


# ---------------------------------------------------------------------------
# find_slug_by_contract
# ---------------------------------------------------------------------------

class TestFindSlugByContract:
    @pytest.mark.asyncio
    async def test_finds_slug_for_matching_contract(self, client):
        response = {
            "data": [
                {
                    "slug": "bonk",
                    "name": "Bonk",
                    "profile": {
                        "contract_addresses": [
                            {"chain": "solana", "contract_address": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"},
                        ]
                    },
                },
                {
                    "slug": "solana",
                    "name": "Solana",
                    "profile": {"contract_addresses": []},
                },
            ]
        }
        with patch.object(client, "_get", new=AsyncMock(return_value=response)):
            slug = await client.find_slug_by_contract(
                "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
            )
        assert slug == "bonk"

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self, client):
        response = {"data": [
            {"slug": "solana", "name": "Solana", "profile": {"contract_addresses": []}}
        ]}
        with patch.object(client, "_get", new=AsyncMock(return_value=response)):
            slug = await client.find_slug_by_contract("0xUnknownAddress")
        assert slug is None

    @pytest.mark.asyncio
    async def test_exception_returns_none(self, client):
        with patch.object(client, "_get", new=AsyncMock(side_effect=Exception("network error"))):
            slug = await client.find_slug_by_contract("some_address")
        assert slug is None
