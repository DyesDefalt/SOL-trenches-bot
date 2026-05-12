"""
Tests for CrossRefValidator.

Uses mocked CoinGeckoClient and optional Messari client.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.intel.crossref_validator import CrossRefValidator


def _make_cg_client(
    contract_data: dict | None = None,
    trending_data: dict | None = None,
):
    """Build a mock CoinGeckoClient."""
    client = MagicMock()
    client.get_token_by_contract = AsyncMock(return_value=contract_data or {})
    client.get_trending = AsyncMock(return_value=trending_data or {"coins": []})
    return client


def _make_messari_client(asset_data: dict | None = None):
    """Build a mock Messari duck-typed client."""
    client = MagicMock()
    client.get_asset = AsyncMock(return_value=asset_data)
    return client


class TestCrossRefValidatorListedAndRanked:
    """Token listed on CoinGecko with a top-500 rank."""

    @pytest.mark.asyncio
    async def test_listed_top500_rank(self):
        """CG listed + rank 300 → legitimacy 10, bonus 10."""
        cg = _make_cg_client(
            contract_data={
                "id": "pepe",
                "symbol": "pepe",
                "market_cap_rank": 300,
                "categories": ["meme-token"],
            }
        )
        validator = CrossRefValidator(coingecko=cg)
        result = await validator.validate_token("TokenAddr123", symbol="PEPE")

        assert result.coingecko_listed is True
        assert result.coingecko_rank == 300
        assert result.coingecko_id == "pepe"
        assert result.category == "meme-token"
        # listed (+5 leg +5 bonus) + rank 300 top500 (+5 leg +5 bonus)
        assert result.legitimacy_score == 10.0   # capped at 10
        assert result.cross_ref_bonus == 10.0
        assert "coingecko_listed" in result.reasons
        assert any("top500" in r for r in result.reasons)

    @pytest.mark.asyncio
    async def test_listed_top100_rank(self):
        """CG listed + rank 50 → legitimacy 10, bonus 15."""
        cg = _make_cg_client(
            contract_data={
                "id": "sol",
                "symbol": "sol",
                "market_cap_rank": 50,
                "categories": ["layer-1"],
            }
        )
        validator = CrossRefValidator(coingecko=cg)
        result = await validator.validate_token("SolAddr", symbol="SOL")

        assert result.coingecko_rank == 50
        # listed (+5) + top100 (+10) = 15 → capped at 10
        assert result.legitimacy_score == 10.0
        # bonus: +5 + +10 = 15
        assert result.cross_ref_bonus == 15.0
        assert any("top100" in r for r in result.reasons)


class TestCrossRefValidatorListedOnly:
    """Token on CoinGecko but no rank (new listing)."""

    @pytest.mark.asyncio
    async def test_listed_no_rank(self):
        """CG listed, no rank yet → legitimacy 5, bonus 5."""
        cg = _make_cg_client(
            contract_data={
                "id": "newmeme",
                "symbol": "NM",
                "market_cap_rank": None,
                "categories": [],
            }
        )
        validator = CrossRefValidator(coingecko=cg)
        result = await validator.validate_token("NewMemeAddr", symbol="NM")

        assert result.coingecko_listed is True
        assert result.coingecko_rank is None
        assert result.legitimacy_score == 5.0
        assert result.cross_ref_bonus == 5.0


class TestCrossRefValidatorNotListed:
    """Token not on CoinGecko or Messari."""

    @pytest.mark.asyncio
    async def test_not_listed_new_token(self):
        """Not listed, age < 7d → no penalty. legitimacy 0, bonus 0."""
        cg = _make_cg_client(contract_data={})
        validator = CrossRefValidator(coingecko=cg)
        result = await validator.validate_token(
            "BrandNewAddr", symbol="BRAND", token_age_days=2.0
        )

        assert result.coingecko_listed is False
        assert result.legitimacy_score == 0.0
        assert result.cross_ref_bonus == 0.0
        assert "not_cross_listed_old_token" not in result.reasons

    @pytest.mark.asyncio
    async def test_not_listed_old_token_penalty(self):
        """Not listed, age > 7d → bonus -3 (suspicious)."""
        cg = _make_cg_client(contract_data={})
        validator = CrossRefValidator(coingecko=cg)
        result = await validator.validate_token(
            "OldScamAddr", symbol="SCAM", token_age_days=30.0
        )

        assert result.coingecko_listed is False
        assert result.cross_ref_bonus == -3.0
        assert "not_cross_listed_old_token" in result.reasons

    @pytest.mark.asyncio
    async def test_no_clients_returns_zero_scores(self):
        """No data sources → legitimacy 0, bonus 0, no crash."""
        validator = CrossRefValidator(coingecko=None, messari=None)
        result = await validator.validate_token("AnyAddr", symbol="ANY")

        assert result.legitimacy_score == 0.0
        assert result.cross_ref_bonus == 0.0
        assert result.coingecko_listed is False


class TestCrossRefValidatorTrendingBoost:
    """Token in CG trending list gets additional bonus."""

    @pytest.mark.asyncio
    async def test_trending_adds_bonus(self):
        """Listed + trending → additional +5 bonus."""
        cg = _make_cg_client(
            contract_data={
                "id": "bonk",
                "symbol": "BONK",
                "market_cap_rank": 200,
                "categories": ["solana-meme"],
            },
            trending_data={
                "coins": [
                    {"item": {"id": "bonk", "symbol": "BONK", "name": "Bonk"}},
                ]
            },
        )
        validator = CrossRefValidator(coingecko=cg)
        result = await validator.validate_token("BonkAddr", symbol="BONK")

        assert result.is_trending is True
        # listed (+5) + top500 (+5) + trending (+5) = 15 → capped at 15
        assert result.cross_ref_bonus == 15.0
        assert "coingecko_trending" in result.reasons

    @pytest.mark.asyncio
    async def test_messari_listed_adds_score(self):
        """Messari listed adds +3 legitimacy and +3 bonus."""
        cg = _make_cg_client(contract_data={})  # not on CG
        messari = _make_messari_client(
            asset_data={"data": {"slug": "my-token", "symbol": "MYT"}}
        )
        validator = CrossRefValidator(coingecko=cg, messari=messari)
        result = await validator.validate_token("MessariAddr", symbol="MYT")

        assert result.messari_listed is True
        assert result.messari_slug == "my-token"
        assert result.legitimacy_score == 3.0
        assert result.cross_ref_bonus == 3.0
        assert "messari_listed" in result.reasons
