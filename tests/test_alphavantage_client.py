"""
Tests for AlphaVantageClient — Phase 9 macro.

Coverage:
- All 4 quote methods return {"price": float, "change_pct": float, "raw": {...}} on success
- "%" stripping in change_percent parsing
- API Note/Information messages (rate limit) handled gracefully
- HTTP errors return {} gracefully
- BTC daily returns raw response with correct key
- Missing "Global Quote" key returns {}
- Malformed price values return {}
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.clients.alphavantage_client import AlphaVantageClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _global_quote_response(
    symbol: str = "SPY",
    price: str = "450.12",
    change_pct: str = "-1.23%",
) -> dict:
    """Simulate Alpha Vantage GLOBAL_QUOTE response."""
    return {
        "Global Quote": {
            "01. symbol": symbol,
            "05. price": price,
            "09. change": "-5.60",
            "10. change percent": change_pct,
        }
    }


def _btc_daily_response() -> dict:
    """Simulate Alpha Vantage DIGITAL_CURRENCY_DAILY response."""
    return {
        "Meta Data": {
            "2. Digital Currency Code": "BTC",
            "4. Market Code": "USD",
        },
        "Time Series (Digital Currency Daily)": {
            "2024-01-02": {"4a. close (USD)": "46000.00", "1a. open (USD)": "45000.00"},
            "2024-01-01": {"4a. close (USD)": "45000.00", "1a. open (USD)": "44000.00"},
        },
    }


@pytest.fixture
def client():
    """AlphaVantageClient with mocked HTTP layer and limiter."""
    c = AlphaVantageClient(api_key="test_av_key")
    c._limiter = AsyncMock()
    c._limiter.acquire = AsyncMock()
    return c


# ---------------------------------------------------------------------------
# Tests: get_spx_quote
# ---------------------------------------------------------------------------

class TestGetSpxQuote:

    @pytest.mark.asyncio
    async def test_success_returns_price_and_change(self, client):
        """SPX quote returns price and change_pct as floats."""
        raw = _global_quote_response("SPY", price="452.50", change_pct="0.85%")
        with patch.object(client._http, "get", new=AsyncMock(return_value=raw)):
            result = await client.get_spx_quote()

        assert result["price"] == pytest.approx(452.50)
        assert result["change_pct"] == pytest.approx(0.85)
        assert "raw" in result

    @pytest.mark.asyncio
    async def test_negative_change_pct_parsed(self, client):
        """Negative change percent (with % sign) is correctly parsed."""
        raw = _global_quote_response("SPY", price="440.00", change_pct="-2.50%")
        with patch.object(client._http, "get", new=AsyncMock(return_value=raw)):
            result = await client.get_spx_quote()

        assert result["change_pct"] == pytest.approx(-2.50)

    @pytest.mark.asyncio
    async def test_api_note_rate_limit_returns_empty(self, client):
        """Alpha Vantage 'Note' (rate limit) returns {} gracefully."""
        raw = {"Note": "Thank you for using Alpha Vantage! API rate limit reached."}
        with patch.object(client._http, "get", new=AsyncMock(return_value=raw)):
            result = await client.get_spx_quote()

        assert result == {}

    @pytest.mark.asyncio
    async def test_http_error_returns_empty(self, client):
        """HTTP error returns {} — fail-safe."""
        from src.clients.base import HTTPError
        with patch.object(
            client._http, "get",
            new=AsyncMock(side_effect=HTTPError("Server error", status=500))
        ):
            result = await client.get_spx_quote()

        assert result == {}

    @pytest.mark.asyncio
    async def test_missing_global_quote_key_returns_empty(self, client):
        """Response without 'Global Quote' key returns {}."""
        raw = {"unexpected": "data"}
        with patch.object(client._http, "get", new=AsyncMock(return_value=raw)):
            result = await client.get_spx_quote()

        assert result == {}


# ---------------------------------------------------------------------------
# Tests: get_dxy_quote
# ---------------------------------------------------------------------------

class TestGetDxyQuote:

    @pytest.mark.asyncio
    async def test_success_returns_price(self, client):
        """DXY (UUP ETF) quote returns price and change_pct."""
        raw = _global_quote_response("UUP", price="29.10", change_pct="0.20%")
        with patch.object(client._http, "get", new=AsyncMock(return_value=raw)):
            result = await client.get_dxy_quote()

        assert result["price"] == pytest.approx(29.10)
        assert result["change_pct"] == pytest.approx(0.20)

    @pytest.mark.asyncio
    async def test_connection_error_returns_empty(self, client):
        """Network error returns {} gracefully."""
        with patch.object(
            client._http, "get",
            new=AsyncMock(side_effect=Exception("connection timeout"))
        ):
            result = await client.get_dxy_quote()

        assert result == {}


# ---------------------------------------------------------------------------
# Tests: get_vix_quote
# ---------------------------------------------------------------------------

class TestGetVixQuote:

    @pytest.mark.asyncio
    async def test_success_returns_vix_price(self, client):
        """VIX (VIXY ETF) quote returns price (the key signal value)."""
        raw = _global_quote_response("VIXY", price="14.55", change_pct="8.50%")
        with patch.object(client._http, "get", new=AsyncMock(return_value=raw)):
            result = await client.get_vix_quote()

        assert result["price"] == pytest.approx(14.55)
        assert result["change_pct"] == pytest.approx(8.50)

    @pytest.mark.asyncio
    async def test_information_key_rate_limit_returns_empty(self, client):
        """Alpha Vantage 'Information' key (rate limit message) returns {}."""
        raw = {"Information": "API key invalid or rate limited."}
        with patch.object(client._http, "get", new=AsyncMock(return_value=raw)):
            result = await client.get_vix_quote()

        assert result == {}


# ---------------------------------------------------------------------------
# Tests: get_btc_daily
# ---------------------------------------------------------------------------

class TestGetBtcDaily:

    @pytest.mark.asyncio
    async def test_success_returns_time_series(self, client):
        """BTC daily returns raw response with time series key present."""
        raw = _btc_daily_response()
        with patch.object(client._http, "get", new=AsyncMock(return_value=raw)):
            result = await client.get_btc_daily()

        assert "Time Series (Digital Currency Daily)" in result
        ts = result["Time Series (Digital Currency Daily)"]
        assert "2024-01-02" in ts
        assert float(ts["2024-01-02"]["4a. close (USD)"]) == pytest.approx(46000.00)

    @pytest.mark.asyncio
    async def test_missing_time_series_key_returns_empty(self, client):
        """Response without time series key returns {}."""
        raw = {"Meta Data": {"2. Digital Currency Code": "BTC"}}
        with patch.object(client._http, "get", new=AsyncMock(return_value=raw)):
            result = await client.get_btc_daily()

        assert result == {}

    @pytest.mark.asyncio
    async def test_api_note_returns_empty(self, client):
        """Alpha Vantage Note (rate limit) on BTC daily returns {}."""
        raw = {"Note": "API rate limit."}
        with patch.object(client._http, "get", new=AsyncMock(return_value=raw)):
            result = await client.get_btc_daily()

        assert result == {}


# ---------------------------------------------------------------------------
# Unit tests: _parse_global_quote
# ---------------------------------------------------------------------------

class TestParseGlobalQuote:
    """Unit tests for the static parser — no network."""

    def test_parses_price_and_strips_percent(self):
        raw = {
            "Global Quote": {
                "05. price": "500.00",
                "10. change percent": "1.50%",
            }
        }
        result = AlphaVantageClient._parse_global_quote(raw)
        assert result["price"] == pytest.approx(500.00)
        assert result["change_pct"] == pytest.approx(1.50)

    def test_negative_change_with_percent_sign(self):
        raw = {
            "Global Quote": {
                "05. price": "100.00",
                "10. change percent": "-3.75%",
            }
        }
        result = AlphaVantageClient._parse_global_quote(raw)
        assert result["change_pct"] == pytest.approx(-3.75)

    def test_empty_global_quote_returns_empty(self):
        assert AlphaVantageClient._parse_global_quote({"Global Quote": {}}) == {}

    def test_malformed_price_returns_empty(self):
        raw = {
            "Global Quote": {
                "05. price": "not_a_number",
                "10. change percent": "1%",
            }
        }
        result = AlphaVantageClient._parse_global_quote(raw)
        assert result == {}
