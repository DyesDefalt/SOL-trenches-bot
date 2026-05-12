"""
Tests for ReflectionAgent — post-trade LLM analysis.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ai.reflection_agent import ReflectionAgent
from src.ai.schemas import TradeReflection


def _make_reflection(classification: str = "PROFITABLE_AS_EXPECTED") -> TradeReflection:
    return TradeReflection(
        trade_classification=classification,
        key_signals=["high smart money count", "strong volume momentum"],
        misleading_signals=[],
        emerging_pattern=None,
        recommended_adjustment=None,
        lesson_summary="High smart money count correlated with profitable exit at TP1.",
    )


class TestReflectionAgent:
    """Unit tests untuk ReflectionAgent."""

    @pytest.mark.asyncio
    async def test_lesson_saved_after_reflection(self):
        """Lesson disimpan ke store setelah LLM response berhasil."""
        mock_llm = MagicMock()
        mock_llm.complete_structured = AsyncMock(
            return_value=_make_reflection("PROFITABLE_AS_EXPECTED")
        )
        mock_store = MagicMock()
        mock_store.add_lesson = AsyncMock()

        with patch("src.ai.reflection_agent.settings") as mock_settings:
            mock_settings.ai_enabled = True
            mock_settings.ai_reflection_enabled = True
            mock_settings.llm_reasoning_model = "anthropic/claude-haiku-4.5"
            mock_settings.llm_timeout_seconds = 10.0

            agent = ReflectionAgent(mock_llm, mock_store)
            await agent.reflect_on_trade(
                position_id=42,
                token_symbol="MOON",
                token_address="MoonToken123",
                entry_score=82.5,
                entry_price_usd=0.000015,
                exit_price_usd=0.000027,
                entry_amount_sol=0.05,
                smart_money_count=4,
                pnl_sol=0.04,
                pnl_pct=80.0,
                holding_minutes=22.5,
                exit_reason="TP1",
            )

        # Store harus dipanggil satu kali
        mock_store.add_lesson.assert_called_once()
        saved = mock_store.add_lesson.call_args[0][0]
        assert saved["position_id"] == 42
        assert saved["token_symbol"] == "MOON"
        assert saved["trade_classification"] == "PROFITABLE_AS_EXPECTED"
        assert "lesson_summary" in saved

    @pytest.mark.asyncio
    async def test_no_lesson_saved_on_llm_failure(self):
        """Store tidak dipanggil kalau LLM return None."""
        mock_llm = MagicMock()
        mock_llm.complete_structured = AsyncMock(return_value=None)
        mock_store = MagicMock()
        mock_store.add_lesson = AsyncMock()

        with patch("src.ai.reflection_agent.settings") as mock_settings:
            mock_settings.ai_enabled = True
            mock_settings.ai_reflection_enabled = True
            mock_settings.llm_reasoning_model = "anthropic/claude-haiku-4.5"
            mock_settings.llm_timeout_seconds = 10.0

            agent = ReflectionAgent(mock_llm, mock_store)
            await agent.reflect_on_trade(
                position_id=99,
                token_symbol="FAIL",
                token_address="FailToken",
                entry_score=70.0,
                entry_price_usd=0.00002,
                exit_price_usd=0.000011,
                entry_amount_sol=0.05,
                smart_money_count=1,
                pnl_sol=-0.025,
                pnl_pct=-50.0,
                holding_minutes=45.0,
                exit_reason="SL",
            )

        # Store tidak dipanggil kalau LLM gagal
        mock_store.add_lesson.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_llm_exception_gracefully(self):
        """LLM exception tidak crash caller — di-swallow dengan log."""
        mock_llm = MagicMock()
        mock_llm.complete_structured = AsyncMock(
            side_effect=RuntimeError("Connection refused")
        )
        mock_store = MagicMock()
        mock_store.add_lesson = AsyncMock()

        with patch("src.ai.reflection_agent.settings") as mock_settings:
            mock_settings.ai_enabled = True
            mock_settings.ai_reflection_enabled = True
            mock_settings.llm_reasoning_model = "anthropic/claude-haiku-4.5"
            mock_settings.llm_timeout_seconds = 10.0

            agent = ReflectionAgent(mock_llm, mock_store)
            # Tidak boleh raise
            await agent.reflect_on_trade(
                position_id=77,
                token_symbol="ERR",
                token_address="ErrorToken",
                entry_score=78.0,
                entry_price_usd=0.00003,
                exit_price_usd=0.000015,
                entry_amount_sol=0.05,
                smart_money_count=2,
                pnl_sol=-0.02,
                pnl_pct=-40.0,
                holding_minutes=30.0,
                exit_reason="SL",
            )

        mock_store.add_lesson.assert_not_called()

    @pytest.mark.asyncio
    async def test_disabled_skips_all_work(self):
        """Kalau ai_enabled=False, tidak ada LLM call dan tidak ada lesson."""
        mock_llm = MagicMock()
        mock_llm.complete_structured = AsyncMock()
        mock_store = MagicMock()
        mock_store.add_lesson = AsyncMock()

        with patch("src.ai.reflection_agent.settings") as mock_settings:
            mock_settings.ai_enabled = False
            mock_settings.ai_reflection_enabled = True

            agent = ReflectionAgent(mock_llm, mock_store)
            await agent.reflect_on_trade(
                position_id=1,
                token_symbol="SKIP",
                token_address="SkipToken",
                entry_score=75.0,
                entry_price_usd=0.00001,
                exit_price_usd=0.000018,
                entry_amount_sol=0.05,
                smart_money_count=2,
                pnl_sol=0.03,
                pnl_pct=60.0,
                holding_minutes=15.0,
                exit_reason="TP1",
            )

        mock_llm.complete_structured.assert_not_called()
        mock_store.add_lesson.assert_not_called()
