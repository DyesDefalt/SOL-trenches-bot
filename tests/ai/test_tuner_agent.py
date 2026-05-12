"""Unit tests untuk TunerAgent (Phase 6c)."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ai.schemas import TunerRecommendation
from src.ai.tuner_agent import TunerAgent

# --- Helpers ---


def _make_position(score: float, pnl: float, exit_reason: str, days_ago: float = 1.0) -> dict:
    """Create synthetic closed position dict."""
    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {
        "id": 1,
        "token_symbol": "TEST",
        "entry_score": score,
        "realized_pnl_sol": pnl,
        "exit_reason": exit_reason,
        "exit_timestamp": ts,
    }


def _make_daily_pnl(days: int = 7) -> list[dict]:
    base_date = datetime.now(timezone.utc).date()
    result = []
    for i in range(days):
        d = base_date - timedelta(days=i)
        result.append({
            "date": d,
            "pnl_sol": 0.02 * (1 if i % 2 == 0 else -1),
            "trades_total": 3,
            "trades_won": 2 if i % 2 == 0 else 1,
            "trades_lost": 1 if i % 2 == 0 else 2,
        })
    return result


_SAMPLE_POSITIONS = [
    _make_position(score=82, pnl=0.04, exit_reason="TP1", days_ago=0.5),
    _make_position(score=78, pnl=-0.02, exit_reason="SL", days_ago=1.0),
    _make_position(score=86, pnl=0.08, exit_reason="TP2", days_ago=1.5),
    _make_position(score=77, pnl=0.01, exit_reason="TIME_EXIT", days_ago=2.0),
    _make_position(score=81, pnl=-0.03, exit_reason="SL", days_ago=2.5),
]

_MOCK_RECOMMENDATION = TunerRecommendation(
    parameter="min_score_to_buy",
    current_value=75.0,
    suggested_value=77.0,
    justification="Score range 75-79 shows 40% WR vs 67% for 80+. Tighten threshold.",
    expected_impact="Reduce low-score trades, improve overall WR by ~5%.",
    confidence=0.80,
    warning_flags=["High SL rate in 75-79 score range"],
)


def _make_tuner(llm_result=_MOCK_RECOMMENDATION, positions=_SAMPLE_POSITIONS):
    """Helper: create TunerAgent with mocked dependencies."""
    llm_mock = AsyncMock()
    llm_mock.complete_structured = AsyncMock(return_value=llm_result)

    db_mock = AsyncMock()
    db_mock.get_recent_closed_positions = AsyncMock(return_value=positions)
    db_mock.get_daily_pnl = AsyncMock(return_value=_make_daily_pnl(7))

    lesson_mock = MagicMock()
    lesson_mock.get_top = MagicMock(return_value=[
        {"lesson": "SL at 75-79 score trades hit frequently"},
        {"lesson": "TP1 exits performing well above 80 score"},
    ])

    return TunerAgent(llm=llm_mock, db=db_mock, lesson_store=lesson_mock), llm_mock, db_mock


# --- Tests ---


@pytest.mark.asyncio
async def test_recommendation_generation() -> None:
    """Normal flow: enough data → LLM called → recommendation returned."""
    agent, llm_mock, db_mock = _make_tuner()
    result = await agent.analyze_weekly_performance()

    assert result is not None
    assert result.parameter == "min_score_to_buy"
    assert result.suggested_value == 77.0
    assert 0.0 <= result.confidence <= 1.0
    llm_mock.complete_structured.assert_called_once()
    db_mock.get_recent_closed_positions.assert_called_once()


@pytest.mark.asyncio
async def test_insufficient_data_returns_none() -> None:
    """Fewer than 3 positions in last 7 days → return None without LLM call."""
    # Positions all older than 7 days
    old_positions = [
        _make_position(score=80, pnl=0.02, exit_reason="TP1", days_ago=10.0),
        _make_position(score=78, pnl=-0.01, exit_reason="SL", days_ago=11.0),
    ]
    agent, llm_mock, _ = _make_tuner(positions=old_positions)
    result = await agent.analyze_weekly_performance()

    assert result is None
    llm_mock.complete_structured.assert_not_called()


@pytest.mark.asyncio
async def test_llm_returns_none() -> None:
    """LLM unavailable (cost cap / error) → agent returns None gracefully."""
    agent, llm_mock, _ = _make_tuner(llm_result=None)
    result = await agent.analyze_weekly_performance()

    assert result is None
    llm_mock.complete_structured.assert_called_once()


@pytest.mark.asyncio
async def test_json_history_persistence(tmp_path: Path) -> None:
    """Recommendation should be saved to tuning_history.json."""
    history_path = tmp_path / "tuning_history.json"

    with patch("src.ai.tuner_agent._TUNING_HISTORY_PATH", history_path):
        TunerAgent.save_to_history(_MOCK_RECOMMENDATION)

    assert history_path.exists()
    saved = json.loads(history_path.read_text())
    assert isinstance(saved, list)
    assert len(saved) == 1
    assert saved[0]["parameter"] == "min_score_to_buy"
    assert saved[0]["suggested_value"] == 77.0
    assert "timestamp" in saved[0]


@pytest.mark.asyncio
async def test_history_fifo_max_50(tmp_path: Path) -> None:
    """History file should not exceed 50 entries (FIFO)."""
    history_path = tmp_path / "tuning_history.json"

    with patch("src.ai.tuner_agent._TUNING_HISTORY_PATH", history_path):
        # Write 55 entries
        for _ in range(55):
            TunerAgent.save_to_history(_MOCK_RECOMMENDATION)

    saved = json.loads(history_path.read_text())
    assert len(saved) == 50  # FIFO cap
