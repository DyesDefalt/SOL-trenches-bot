"""
Tests for CryptoQuantClient — Phase 9 macro.

Coverage:
- All 4 methods return normalized {"data": [...], "status": "ok"} on success
- 401 / 403 auth errors return {} gracefully (no raise)
- Generic HTTP errors return {} gracefully
- Network/connection errors return {} gracefully
- Response normalization handles various CryptoQuant shapes
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.clients.cryptoquant_client import CryptoQuantClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_cq_response(data: list | None = None) -> dict:
    """Simulate a typical CryptoQuant API response shape."""
    return {
        "result": {
            "data": data or [
                {"date": "2024-01-01T00:00:00Z", "netflow": 1234.5},
                {"date": "2024-01-01T01:00:00Z", "netflow": -567.8},
            ]
        }
    }


@pytest.fixture
def client():
    """CryptoQuantClient with a mocked HTTP layer and limiter that doesn't wait."""
    c = CryptoQuantClient(api_key="test_key")
    # Patch the rate limiter so tests don't actually sleep
    c._limiter = AsyncMock()
    c._limiter.acquire = AsyncMock()
    return c


# ---------------------------------------------------------------------------
# Tests: get_btc_exchange_flows
# ---------------------------------------------------------------------------

class TestGetBtcExchangeFlows:

    @pytest.mark.asyncio
    async def test_success_returns_normalized(self, client):
        """Successful call returns {"data": [...], "status": "ok"}."""
        raw = _make_cq_response([{"date": "2024-01-01", "netflow": 100.0}])
        with patch.object(client._http, "get", new=AsyncMock(return_value=raw)):
            result = await client.get_btc_exchange_flows()

        assert result["status"] == "ok"
        assert isinstance(result["data"], list)
        assert len(result["data"]) == 1

    @pytest.mark.asyncio
    async def test_401_returns_empty(self, client):
        """401 Unauthorized returns {} — fail-safe."""
        from src.clients.base import HTTPError
        with patch.object(
            client._http, "get",
            new=AsyncMock(side_effect=HTTPError("Unauthorized", status=401))
        ):
            result = await client.get_btc_exchange_flows()

        assert result == {}

    @pytest.mark.asyncio
    async def test_403_returns_empty(self, client):
        """403 Forbidden (Pro tier endpoint) returns {} — fail-safe."""
        from src.clients.base import HTTPError
        with patch.object(
            client._http, "get",
            new=AsyncMock(side_effect=HTTPError("Forbidden", status=403))
        ):
            result = await client.get_btc_exchange_flows()

        assert result == {}

    @pytest.mark.asyncio
    async def test_network_error_returns_empty(self, client):
        """Connection error returns {} — fail-safe."""
        with patch.object(
            client._http, "get",
            new=AsyncMock(side_effect=Exception("connection refused"))
        ):
            result = await client.get_btc_exchange_flows()

        assert result == {}


# ---------------------------------------------------------------------------
# Tests: get_btc_mvrv_ratio
# ---------------------------------------------------------------------------

class TestGetBtcMvrvRatio:

    @pytest.mark.asyncio
    async def test_success_returns_normalized(self, client):
        """MVRV endpoint returns normalized dict."""
        raw = _make_cq_response([{"date": "2024-01-01", "mvrv_ratio": 2.45}])
        with patch.object(client._http, "get", new=AsyncMock(return_value=raw)):
            result = await client.get_btc_mvrv_ratio()

        assert result["status"] == "ok"
        assert result["data"][0]["mvrv_ratio"] == 2.45

    @pytest.mark.asyncio
    async def test_403_on_mvrv_returns_empty(self, client):
        """Free plan 403 on MVRV returns {} (Pro endpoint)."""
        from src.clients.base import HTTPError
        with patch.object(
            client._http, "get",
            new=AsyncMock(side_effect=HTTPError("Forbidden", status=403))
        ):
            result = await client.get_btc_mvrv_ratio()

        assert result == {}


# ---------------------------------------------------------------------------
# Tests: get_btc_funding_rates
# ---------------------------------------------------------------------------

class TestGetBtcFundingRates:

    @pytest.mark.asyncio
    async def test_success_returns_normalized(self, client):
        """Funding rates endpoint returns normalized dict."""
        raw = _make_cq_response([{"date": "2024-01-01", "funding_rate": 0.0001}])
        with patch.object(client._http, "get", new=AsyncMock(return_value=raw)):
            result = await client.get_btc_funding_rates()

        assert result["status"] == "ok"
        assert isinstance(result["data"], list)

    @pytest.mark.asyncio
    async def test_empty_response_normalizes_to_empty_data(self, client):
        """Empty API response normalizes to {} (not a crash)."""
        with patch.object(client._http, "get", new=AsyncMock(return_value={})):
            result = await client.get_btc_funding_rates()

        # _normalize({}) returns {}
        assert result == {}


# ---------------------------------------------------------------------------
# Tests: get_btc_coinbase_premium
# ---------------------------------------------------------------------------

class TestGetBtcCoinbasePremium:

    @pytest.mark.asyncio
    async def test_success_returns_normalized(self, client):
        """Coinbase premium endpoint returns normalized dict."""
        raw = _make_cq_response([{"date": "2024-01-01", "coinbase_premium_index": 0.5}])
        with patch.object(client._http, "get", new=AsyncMock(return_value=raw)):
            result = await client.get_btc_coinbase_premium()

        assert result["status"] == "ok"
        assert result["data"][0]["coinbase_premium_index"] == 0.5

    @pytest.mark.asyncio
    async def test_401_on_coinbase_premium_returns_empty(self, client):
        """401 on Coinbase premium returns {} gracefully."""
        from src.clients.base import HTTPError
        with patch.object(
            client._http, "get",
            new=AsyncMock(side_effect=HTTPError("Unauthorized", status=401))
        ):
            result = await client.get_btc_coinbase_premium()

        assert result == {}


# ---------------------------------------------------------------------------
# Tests: _normalize helper
# ---------------------------------------------------------------------------

class TestNormalize:
    """Unit tests for the response normalizer (no network calls needed)."""

    def test_result_key_with_data_list(self):
        """Standard CryptoQuant shape: {"result": {"data": [...]}}."""
        raw = {"result": {"data": [{"x": 1}, {"x": 2}]}}
        result = CryptoQuantClient._normalize(raw)
        assert result["status"] == "ok"
        assert result["data"] == [{"x": 1}, {"x": 2}]

    def test_flat_data_key(self):
        """Alternate shape: {"data": [...]}."""
        raw = {"data": [{"y": 3}]}
        result = CryptoQuantClient._normalize(raw)
        assert result["data"] == [{"y": 3}]

    def test_empty_dict_returns_empty(self):
        """Empty dict returns {}."""
        assert CryptoQuantClient._normalize({}) == {}

    def test_non_list_data_wrapped_in_list(self):
        """If data is a dict (not list), it gets wrapped."""
        raw = {"data": {"value": 42}}
        result = CryptoQuantClient._normalize(raw)
        assert result["data"] == [{"value": 42}]
