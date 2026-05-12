"""
Tests for RugCheckAgent — pre-trade LLM rug detection.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ai.rug_check_agent import RugCheckAgent
from src.ai.schemas import RugCheckResult


def _make_approve_result() -> RugCheckResult:
    return RugCheckResult(
        veto=False,
        confidence=0.85,
        reason="Token looks clean with good distribution",
        red_flags=[],
        recommendation="APPROVE",
    )


def _make_veto_result() -> RugCheckResult:
    return RugCheckResult(
        veto=True,
        confidence=0.92,
        reason="Dev wallet holds 45%, LP not burned, fresh wallet LP",
        red_flags=["dev_holding_high", "lp_not_burned", "fresh_lp_wallet"],
        recommendation="VETO",
    )


class TestRugCheckAgent:
    """Unit tests untuk RugCheckAgent."""

    @pytest.mark.asyncio
    async def test_approve_result_returned(self):
        """APPROVE result dikembalikan dengan benar ke caller."""
        mock_llm = MagicMock()
        mock_llm.complete_structured = AsyncMock(return_value=_make_approve_result())

        with patch("src.ai.rug_check_agent.settings") as mock_settings:
            mock_settings.ai_enabled = True
            mock_settings.ai_rug_check_enabled = True
            mock_settings.llm_fast_model = "google/gemini-2.0-flash"
            mock_settings.llm_timeout_seconds = 10.0

            agent = RugCheckAgent(mock_llm)
            result = await agent.assess(
                token_address="TokenAddr123",
                symbol="PEPE",
                mcap_usd=50_000,
                liquidity_usd=15_000,
                age_minutes=45,
                holder_count=800,
                top10_pct=35.0,
                dev_holding_pct=2.5,
                bundle_supply_pct=5.0,
                lp_burned=True,
                is_renounced=True,
                gmgn_security_score=85,
                smart_money_count=3,
                smart_money_buyers=["wallet1", "wallet2", "wallet3"],
            )

        assert result is not None
        assert result.recommendation == "APPROVE"
        assert result.veto is False
        assert result.confidence == 0.85

    @pytest.mark.asyncio
    async def test_veto_triggered_high_confidence(self):
        """VETO dengan confidence tinggi dikembalikan ke caller."""
        mock_llm = MagicMock()
        mock_llm.complete_structured = AsyncMock(return_value=_make_veto_result())

        with patch("src.ai.rug_check_agent.settings") as mock_settings:
            mock_settings.ai_enabled = True
            mock_settings.ai_rug_check_enabled = True
            mock_settings.llm_fast_model = "google/gemini-2.0-flash"
            mock_settings.llm_timeout_seconds = 10.0

            agent = RugCheckAgent(mock_llm)
            result = await agent.assess(
                token_address="ScamToken999",
                symbol="RUG",
                mcap_usd=30_000,
                liquidity_usd=5_000,
                age_minutes=5,
                holder_count=50,
                top10_pct=85.0,
                dev_holding_pct=45.0,
                bundle_supply_pct=60.0,
                lp_burned=False,
                is_renounced=False,
                gmgn_security_score=30,
                smart_money_count=0,
                smart_money_buyers=[],
            )

        assert result is not None
        assert result.veto is True
        assert result.recommendation == "VETO"
        assert result.confidence == 0.92
        assert len(result.red_flags) == 3

    @pytest.mark.asyncio
    async def test_handles_llm_none_gracefully(self):
        """None dari LLM (LLM down / cost cap) dikembalikan tanpa crash."""
        mock_llm = MagicMock()
        mock_llm.complete_structured = AsyncMock(return_value=None)

        with patch("src.ai.rug_check_agent.settings") as mock_settings:
            mock_settings.ai_enabled = True
            mock_settings.ai_rug_check_enabled = True
            mock_settings.llm_fast_model = "google/gemini-2.0-flash"
            mock_settings.llm_timeout_seconds = 10.0

            agent = RugCheckAgent(mock_llm)
            result = await agent.assess(
                token_address="TokenXYZ",
                symbol="TEST",
                mcap_usd=40_000,
                liquidity_usd=10_000,
                age_minutes=20,
                holder_count=400,
                top10_pct=40.0,
                dev_holding_pct=5.0,
                bundle_supply_pct=10.0,
                lp_burned=True,
                is_renounced=True,
                gmgn_security_score=75,
                smart_money_count=2,
                smart_money_buyers=["w1", "w2"],
            )

        # None dikembalikan — caller harus fallback ke static rules
        assert result is None

    @pytest.mark.asyncio
    async def test_disabled_when_ai_flag_off(self):
        """Agent langsung return None kalau ai_enabled=False."""
        mock_llm = MagicMock()
        mock_llm.complete_structured = AsyncMock(return_value=_make_approve_result())

        with patch("src.ai.rug_check_agent.settings") as mock_settings:
            mock_settings.ai_enabled = False
            mock_settings.ai_rug_check_enabled = True

            agent = RugCheckAgent(mock_llm)
            result = await agent.assess(
                token_address="TokenXYZ",
                symbol="TEST",
                mcap_usd=40_000,
                liquidity_usd=10_000,
                age_minutes=20,
                holder_count=400,
                top10_pct=40.0,
                dev_holding_pct=5.0,
                bundle_supply_pct=10.0,
                lp_burned=True,
                is_renounced=True,
                gmgn_security_score=75,
                smart_money_count=2,
                smart_money_buyers=["w1"],
            )

        # LLM tidak dipanggil sama sekali
        mock_llm.complete_structured.assert_not_called()
        assert result is None

    @pytest.mark.asyncio
    async def test_schema_validation_rejects_bad_result(self):
        """LLMClient yang return invalid schema (via None) tidak crash caller."""
        mock_llm = MagicMock()
        # LLMClient sudah handle validation error → return None
        mock_llm.complete_structured = AsyncMock(return_value=None)

        with patch("src.ai.rug_check_agent.settings") as mock_settings:
            mock_settings.ai_enabled = True
            mock_settings.ai_rug_check_enabled = True
            mock_settings.llm_fast_model = "google/gemini-2.0-flash"
            mock_settings.llm_timeout_seconds = 10.0

            agent = RugCheckAgent(mock_llm)
            result = await agent.assess(
                token_address="TokenBad",
                symbol="BAD",
                mcap_usd=20_000,
                liquidity_usd=5_000,
                age_minutes=10,
                holder_count=100,
                top10_pct=50.0,
                dev_holding_pct=10.0,
                bundle_supply_pct=15.0,
                lp_burned=False,
                is_renounced=False,
                gmgn_security_score=60,
                smart_money_count=1,
                smart_money_buyers=["w1"],
            )

        # Must handle gracefully
        assert result is None
