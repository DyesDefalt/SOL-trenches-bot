"""
Tests for CoinGeckoClient.

All HTTP calls are mocked — no real network I/O.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.clients.coingecko_client import CoinGeckoClient


def _make_http_client_mock(return_value: dict | list | None = None, raise_exc=None):
    """Return a BaseHTTPClient-shaped mock."""
    mock = MagicMock()
    if raise_exc is not None:
        mock.get = AsyncMock(side_effect=raise_exc)
    else:
        mock.get = AsyncMock(return_value=return_value or {})
    mock.aclose = AsyncMock()
    return mock


class TestCoinGeckoClientInit:
    """Test API key detection and header selection."""

    def test_demo_key_uses_demo_url_and_header(self):
        """CG- prefix key → demo URL and x-cg-demo-api-key header."""
        client = CoinGeckoClient(api_key="CG-demokey123")
        assert "api.coingecko.com" in client._http.base_url
        assert client._http._headers.get("x-cg-demo-api-key") == "CG-demokey123"
        assert "x-cg-pro-api-key" not in client._http._headers

    def test_pro_key_uses_pro_url_and_header(self):
        """Non-CG- key → pro URL and x-cg-pro-api-key header."""
        client = CoinGeckoClient(api_key="pro_secret_key_xyz")
        assert "pro-api.coingecko.com" in client._http.base_url
        assert client._http._headers.get("x-cg-pro-api-key") == "pro_secret_key_xyz"
        assert "x-cg-demo-api-key" not in client._http._headers

    def test_no_key_uses_demo_url(self):
        """No key → demo URL, no auth header."""
        client = CoinGeckoClient(api_key="")
        assert "api.coingecko.com" in client._http.base_url
        assert "x-cg-demo-api-key" not in client._http._headers
        assert "x-cg-pro-api-key" not in client._http._headers


class TestGetTokenByContract:
    """Test contract address lookup."""

    @pytest.mark.asyncio
    async def test_returns_token_data_when_listed(self):
        """Listed token returns full data dict."""
        token_data = {
            "id": "pepe",
            "symbol": "pepe",
            "name": "Pepe",
            "market_cap_rank": 50,
            "categories": ["meme-token"],
        }
        client = CoinGeckoClient(api_key="CG-test")
        client._http = _make_http_client_mock(return_value=token_data)
        client._limiter = MagicMock()
        client._limiter.acquire = AsyncMock()

        result = await client.get_token_by_contract("TokenAddr123")
        assert result["id"] == "pepe"
        assert result["market_cap_rank"] == 50

    @pytest.mark.asyncio
    async def test_returns_empty_on_404(self):
        """404 (not listed) returns {} silently — no exception."""
        from src.clients.base import HTTPError
        client = CoinGeckoClient(api_key="CG-test")
        client._http = _make_http_client_mock(raise_exc=HTTPError("not found", status=404))
        client._limiter = MagicMock()
        client._limiter.acquire = AsyncMock()

        result = await client.get_token_by_contract("NewMemecoinAddr")
        assert result == {}

    @pytest.mark.asyncio
    async def test_returns_empty_on_401(self):
        """401 (auth error) returns {} and logs error."""
        from src.clients.base import HTTPError
        client = CoinGeckoClient(api_key="CG-bad-key")
        client._http = _make_http_client_mock(raise_exc=HTTPError("unauthorized", status=401))
        client._limiter = MagicMock()
        client._limiter.acquire = AsyncMock()

        result = await client.get_token_by_contract("AnyAddr")
        assert result == {}

    @pytest.mark.asyncio
    async def test_uses_solana_platform_by_default(self):
        """Default platform is 'solana'."""
        client = CoinGeckoClient(api_key="CG-test")
        client._http = _make_http_client_mock(return_value={"id": "sol-token"})
        client._limiter = MagicMock()
        client._limiter.acquire = AsyncMock()

        await client.get_token_by_contract("SolAddr123")
        call_args = client._http.get.call_args
        assert "solana" in call_args[0][0]


class TestGetTrending:
    """Test trending coins fetch."""

    @pytest.mark.asyncio
    async def test_returns_trending_list(self):
        """Returns trending structure with coins list."""
        trending = {
            "coins": [
                {"item": {"id": "bonk", "symbol": "BONK", "name": "Bonk"}},
                {"item": {"id": "dogwifhat", "symbol": "WIF", "name": "dogwifhat"}},
            ]
        }
        client = CoinGeckoClient(api_key="CG-test")
        client._http = _make_http_client_mock(return_value=trending)
        client._limiter = MagicMock()
        client._limiter.acquire = AsyncMock()

        result = await client.get_trending()
        assert len(result["coins"]) == 2
        assert result["coins"][0]["item"]["symbol"] == "BONK"

    @pytest.mark.asyncio
    async def test_returns_empty_on_error(self):
        """Returns {} on HTTP error."""
        from src.clients.base import HTTPError
        client = CoinGeckoClient(api_key="CG-test")
        client._http = _make_http_client_mock(raise_exc=HTTPError("server error", status=500))
        client._limiter = MagicMock()
        client._limiter.acquire = AsyncMock()

        result = await client.get_trending()
        assert result == {}


class TestGetSimplePrice:
    """Test simple price fetch."""

    @pytest.mark.asyncio
    async def test_returns_price_data(self):
        """Returns price dict for requested coins."""
        price_data = {"bitcoin": {"usd": 95000.0}, "solana": {"usd": 180.0}}
        client = CoinGeckoClient(api_key="CG-test")
        client._http = _make_http_client_mock(return_value=price_data)
        client._limiter = MagicMock()
        client._limiter.acquire = AsyncMock()

        result = await client.get_simple_price(["bitcoin", "solana"])
        assert result["bitcoin"]["usd"] == 95000.0
        assert result["solana"]["usd"] == 180.0

    @pytest.mark.asyncio
    async def test_returns_empty_on_error(self):
        """Returns {} on HTTP error."""
        from src.clients.base import HTTPError
        client = CoinGeckoClient(api_key="CG-test")
        client._http = _make_http_client_mock(raise_exc=HTTPError("rate limited", status=429))
        client._limiter = MagicMock()
        client._limiter.acquire = AsyncMock()

        result = await client.get_simple_price(["bitcoin"])
        assert result == {}


class TestSearch:
    """Test symbol/name search."""

    @pytest.mark.asyncio
    async def test_search_returns_coins(self):
        """Search returns coins list."""
        search_result = {
            "coins": [
                {"id": "pepe", "symbol": "PEPE", "name": "Pepe"},
            ]
        }
        client = CoinGeckoClient(api_key="CG-test")
        client._http = _make_http_client_mock(return_value=search_result)
        client._limiter = MagicMock()
        client._limiter.acquire = AsyncMock()

        result = await client.search("PEPE")
        assert result["coins"][0]["symbol"] == "PEPE"
