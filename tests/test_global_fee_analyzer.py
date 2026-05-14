"""
Tests for GlobalFeeAnalyzer.

Coverage:
  1. WASH_TRADING when ratio < 0.4
  2. SUSPICIOUS when ratio 0.4–0.7
  3. ORGANIC when ratio 0.7–1.3
  4. UNUSUAL when ratio > 1.5
  5. UNKNOWN when no pair data found
  6. UNKNOWN when fees field absent from pair
  7. UNKNOWN when volume is zero
  8. Score adjustments match spec for each label
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.intel.global_fee_analyzer import FeeAnalysis, GlobalFeeAnalyzer, _classify_ratio

TOKEN = "FeeTestToken222"


def _make_dex(pair: dict | None) -> AsyncMock:
    """Dexscreener mock returning a single pair (or None)."""
    client = AsyncMock()
    client.get_top_pair_for_token.return_value = pair
    return client


def _pair(volume_h1: float, fee_h1: float | None = None) -> dict:
    """Build a minimal DexScreener pair dict."""
    p: dict = {"volume": {"h1": volume_h1}}
    if fee_h1 is not None:
        p["fees"] = {"h1": fee_h1}
    return p


# ── unit tests: _classify_ratio ─────────────────────────────────────────────

class TestClassifyRatio:
    def test_wash_trading_below_0_4(self):
        assert _classify_ratio(0.1) == "WASH_TRADING"
        assert _classify_ratio(0.39) == "WASH_TRADING"

    def test_suspicious_between_0_4_and_0_7(self):
        assert _classify_ratio(0.4) == "SUSPICIOUS"
        assert _classify_ratio(0.65) == "SUSPICIOUS"

    def test_organic_between_0_7_and_1_3(self):
        assert _classify_ratio(0.7) == "ORGANIC"
        assert _classify_ratio(1.0) == "ORGANIC"
        assert _classify_ratio(1.3) == "ORGANIC"

    def test_unusual_above_1_3(self):
        assert _classify_ratio(1.31) == "UNUSUAL"
        assert _classify_ratio(5.0) == "UNUSUAL"


# ── integration tests: GlobalFeeAnalyzer.analyze ────────────────────────────

class TestGlobalFeeAnalyzer:

    @pytest.mark.asyncio
    async def test_wash_trading_label_and_score(self):
        """ratio=0.1 → WASH_TRADING, score=-10."""
        # volume=100k, expected_fee=250; actual_fee=25 → ratio=0.1
        dex = _make_dex(_pair(volume_h1=100_000.0, fee_h1=25.0))
        analyzer = GlobalFeeAnalyzer(dex)
        result = await analyzer.analyze(TOKEN)

        assert result.label == "WASH_TRADING"
        assert result.score_adjustment == -10.0
        assert result.fee_volume_ratio == pytest.approx(0.1)

    @pytest.mark.asyncio
    async def test_suspicious_label_and_score(self):
        """ratio=0.5 → SUSPICIOUS, score=-5."""
        # volume=100k, expected_fee=250; actual_fee=125 → ratio=0.5
        dex = _make_dex(_pair(volume_h1=100_000.0, fee_h1=125.0))
        analyzer = GlobalFeeAnalyzer(dex)
        result = await analyzer.analyze(TOKEN)

        assert result.label == "SUSPICIOUS"
        assert result.score_adjustment == -5.0

    @pytest.mark.asyncio
    async def test_organic_label_and_score(self):
        """ratio=1.0 → ORGANIC, score=+5."""
        # volume=100k, expected_fee=250; actual_fee=250 → ratio=1.0
        dex = _make_dex(_pair(volume_h1=100_000.0, fee_h1=250.0))
        analyzer = GlobalFeeAnalyzer(dex)
        result = await analyzer.analyze(TOKEN)

        assert result.label == "ORGANIC"
        assert result.score_adjustment == 5.0
        assert result.fee_volume_ratio == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_unusual_label_zero_score(self):
        """ratio=2.0 → UNUSUAL, score=0."""
        # volume=100k, expected_fee=250; actual_fee=500 → ratio=2.0
        dex = _make_dex(_pair(volume_h1=100_000.0, fee_h1=500.0))
        analyzer = GlobalFeeAnalyzer(dex)
        result = await analyzer.analyze(TOKEN)

        assert result.label == "UNUSUAL"
        assert result.score_adjustment == 0.0

    @pytest.mark.asyncio
    async def test_unknown_when_no_pair_found(self):
        """If DexScreener returns no pair, label=UNKNOWN, score=0."""
        dex = _make_dex(None)
        analyzer = GlobalFeeAnalyzer(dex)
        result = await analyzer.analyze(TOKEN)

        assert result.label == "UNKNOWN"
        assert result.score_adjustment == 0.0
        assert result.fee_volume_ratio == 0.0

    @pytest.mark.asyncio
    async def test_unknown_when_fees_field_absent(self):
        """Pair exists but fees not reported → UNKNOWN."""
        dex = _make_dex(_pair(volume_h1=100_000.0, fee_h1=None))  # no fees key
        analyzer = GlobalFeeAnalyzer(dex)
        result = await analyzer.analyze(TOKEN)

        assert result.label == "UNKNOWN"

    @pytest.mark.asyncio
    async def test_unknown_when_volume_is_zero(self):
        """Volume = 0 → cannot compute ratio → UNKNOWN."""
        dex = _make_dex(_pair(volume_h1=0.0, fee_h1=0.0))
        analyzer = GlobalFeeAnalyzer(dex)
        result = await analyzer.analyze(TOKEN)

        assert result.label == "UNKNOWN"

    @pytest.mark.asyncio
    async def test_dexscreener_exception_returns_unknown(self):
        """If DexScreener client raises, gracefully return UNKNOWN."""
        dex = AsyncMock()
        dex.get_top_pair_for_token.side_effect = Exception("connection_error")
        analyzer = GlobalFeeAnalyzer(dex)
        result = await analyzer.analyze(TOKEN)

        assert result.label == "UNKNOWN"
        assert result.score_adjustment == 0.0
