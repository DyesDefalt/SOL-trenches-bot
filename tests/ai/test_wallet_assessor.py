"""Unit tests untuk WalletAssessor (Phase 6b)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ai.privacy_filter import PrivacyFilter
from src.ai.schemas import WalletAssessment
from src.ai.wallet_assessor import WalletAssessor

# --- Fixtures ---

_MOCK_STATS = {
    "winrate": 0.72,
    "realized_profit": 45.0,
    "total_profit": 48.0,
    "buy_count": 25,
    "sell_count": 24,
    "token_num": 18,
}

_MOCK_ACTIVITY_FULL = [
    {
        "side": "buy",
        "token_symbol": f"TOKEN{i}",
        "amount_usd": 100 + i * 10,
        "timestamp": 1700000000 + i * 3600,
        "hold_minutes": 30 + i * 5,
    }
    for i in range(10)
]

_ALPHA_ASSESSMENT = WalletAssessment(
    classification="ALPHA_TRADER",
    recommended_tier="A",
    confidence=0.90,
    compatible_with_bot=True,
    avg_hold_minutes=45,
    diversification_score=8,
    reason="Consistent profits, swing 30-60min hold, diversified tokens.",
)

_WASH_ASSESSMENT = WalletAssessment(
    classification="WASH_TRADER",
    recommended_tier="BLACKLIST",
    confidence=0.92,
    compatible_with_bot=False,
    avg_hold_minutes=2,
    diversification_score=1,
    reason="Split-modal pattern detected across multiple wallet clusters.",
)


def _make_assessor(llm_result=_ALPHA_ASSESSMENT, stats=_MOCK_STATS, activity=_MOCK_ACTIVITY_FULL):
    """Helper: create WalletAssessor with mocked dependencies."""
    llm_mock = AsyncMock()
    llm_mock.complete_structured = AsyncMock(return_value=llm_result)

    gmgn_mock = AsyncMock()
    gmgn_mock.get_wallet_stats = AsyncMock(return_value=stats)
    gmgn_mock.get_wallet_activity = AsyncMock(return_value=activity)

    return WalletAssessor(llm=llm_mock, gmgn_client=gmgn_mock), llm_mock, gmgn_mock


# --- Tests ---


@pytest.mark.asyncio
async def test_alpha_trader_classification() -> None:
    """Wallet dengan high winrate + diverse activity → ALPHA_TRADER tier A."""
    # Patch cache para tidak perlu Redis
    with patch("src.ai.wallet_assessor.cached", lambda **kw: lambda f: f):
        assessor, llm_mock, _ = _make_assessor(llm_result=_ALPHA_ASSESSMENT)
        result = await assessor.assess("So1111111111111111111111111111111111111112345", chain="sol")

    assert result is not None
    assert result.classification == "ALPHA_TRADER"
    assert result.recommended_tier == "A"
    assert result.confidence >= 0.85
    llm_mock.complete_structured.assert_called_once()


@pytest.mark.asyncio
async def test_wash_trader_detection() -> None:
    """Wallet dengan wash trade signals → WASH_TRADER + BLACKLIST tier."""
    with patch("src.ai.wallet_assessor.cached", lambda **kw: lambda f: f):
        assessor, llm_mock, _ = _make_assessor(llm_result=_WASH_ASSESSMENT)
        result = await assessor.assess("WashTraderAddr111111111111111111111111111111", chain="sol")

    assert result is not None
    assert result.classification == "WASH_TRADER"
    assert result.recommended_tier == "BLACKLIST"
    assert result.compatible_with_bot is False


@pytest.mark.asyncio
async def test_insufficient_data_returns_none() -> None:
    """Wallet dengan < 5 trades → return None tanpa LLM call."""
    with patch("src.ai.wallet_assessor.cached", lambda **kw: lambda f: f):
        # Hanya 3 trades — di bawah minimum 5
        assessor, llm_mock, _ = _make_assessor(activity=_MOCK_ACTIVITY_FULL[:3])
        result = await assessor.assess("SparseWalletAddr1111111111111111111111111111", chain="sol")

    assert result is None
    # LLM seharusnya TIDAK dipanggil
    llm_mock.complete_structured.assert_not_called()


@pytest.mark.asyncio
async def test_empty_stats_returns_none() -> None:
    """Empty stats dari GMGN → return None tanpa LLM call."""
    with patch("src.ai.wallet_assessor.cached", lambda **kw: lambda f: f):
        assessor, llm_mock, _ = _make_assessor(stats={})
        result = await assessor.assess("EmptyStatsWallet111111111111111111111111111", chain="sol")

    assert result is None
    llm_mock.complete_structured.assert_not_called()


@pytest.mark.asyncio
async def test_privacy_filter_applied() -> None:
    """
    Pastikan PrivacyFilter dipanggil — context dengan API key / secret
    di-strip sebelum dikirim ke LLM.
    """
    captured_context: list[dict] = []

    # Override sanitize_context untuk verifikasi dipanggil
    original_sanitize = PrivacyFilter.sanitize_context

    @staticmethod
    def mock_sanitize(ctx):
        captured_context.append(ctx)
        return original_sanitize(ctx)

    with patch("src.ai.wallet_assessor.PrivacyFilter.sanitize_context", mock_sanitize):
        with patch("src.ai.wallet_assessor.cached", lambda **kw: lambda f: f):
            # Inject a "secret" key into stats to verify it gets stripped
            stats_with_secret = {**_MOCK_STATS, "api_key": "sk_secret123"}
            assessor, _, gmgn_mock = _make_assessor()
            gmgn_mock.get_wallet_stats = AsyncMock(return_value=stats_with_secret)
            await assessor.assess("So1111111111111111111111111111111111111112345", chain="sol")

    # Sanitize should have been called
    assert len(captured_context) >= 1
    # api_key should be stripped from the sanitized result
    sanitized = original_sanitize(captured_context[0])
    assert "api_key" not in sanitized.get("stats_30d", {})


@pytest.mark.asyncio
async def test_llm_unavailable_returns_none() -> None:
    """LLM returns None (e.g., cost cap) → assessor juga return None."""
    with patch("src.ai.wallet_assessor.cached", lambda **kw: lambda f: f):
        assessor, llm_mock, _ = _make_assessor(llm_result=None)
        result = await assessor.assess("ValidWalletAddr111111111111111111111111111111", chain="sol")

    assert result is None
