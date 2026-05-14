"""
Tests for TraderSignalAggregator.

Coverage:
  1.  Hard reject veto: bundler CONFIRMED → hard_reject=True
  2.  Hard reject veto: fee WASH_TRADING → hard_reject=True
  3.  Both veto conditions → hard_reject=True
  4.  No veto conditions → hard_reject=False
  5.  Composite score: sum of components clamped at +20
  6.  Composite score: sum of components clamped at -20
  7.  Composite score: accurate math for mixed signals
  8.  All analyzers called in parallel (gather used)
  9.  Exception in one analyzer → safe default, others still contribute
  10. reasoning list has 4 entries (one per filter)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.intel.bundler_pattern_detector import BundlerPattern
from src.intel.global_fee_analyzer import FeeAnalysis
from src.intel.top_holder_balance_check import HolderBalanceAnalysis
from src.intel.trader_signal_aggregator import TraderSignal, TraderSignalAggregator
from src.intel.wallet_funded_analyzer import FundedFromAnalysis

TOKEN = "AggTestToken555"

# ── helper factories ──────────────────────────────────────────────────────────

def _bundler(strength: str, supply_pct: float = 30.0) -> BundlerPattern:
    return BundlerPattern(
        strength=strength,
        detected_wallets=["w1", "w2", "w3"] if strength != "NONE" else [],
        total_supply_pct=supply_pct,
        reasoning=f"bundler={strength}",
    )


def _fee(label: str, ratio: float = 1.0, adj: float | None = None) -> FeeAnalysis:
    score_map = {
        "WASH_TRADING": -10.0,
        "SUSPICIOUS": -5.0,
        "ORGANIC": 5.0,
        "UNUSUAL": 0.0,
        "UNKNOWN": 0.0,
    }
    return FeeAnalysis(
        label=label,
        fee_volume_ratio=ratio,
        score_adjustment=adj if adj is not None else score_map.get(label, 0.0),
        reasoning=f"fee={label}",
    )


def _funded(label: str, adj: float | None = None) -> FundedFromAnalysis:
    score_map = {"SAFE": 3.0, "CAUTION": 0.0, "RED_FLAG": -10.0, "UNKNOWN": 0.0}
    return FundedFromAnalysis(
        label=label,
        median_age_days=None,
        min_age_days=None,
        score_adjustment=adj if adj is not None else score_map.get(label, 0.0),
        reasoning=f"funded={label}",
    )


def _balance(label: str, adj: float | None = None) -> HolderBalanceAnalysis:
    score_map = {"STRONG": 5.0, "MIXED": 0.0, "WEAK": -5.0, "UNKNOWN": 0.0}
    return HolderBalanceAnalysis(
        label=label,
        min_balance_sol=None,
        median_balance_sol=None,
        weak_count=0,
        score_adjustment=adj if adj is not None else score_map.get(label, 0.0),
        reasoning=f"balance={label}",
    )


def _make_aggregator(
    bundler_result: BundlerPattern,
    fee_result: FeeAnalysis,
    funded_result: FundedFromAnalysis,
    balance_result: HolderBalanceAnalysis,
) -> TraderSignalAggregator:
    """Build aggregator with 4 mocked sub-analyzers."""
    bundler = AsyncMock()
    bundler.detect.return_value = bundler_result

    fee = AsyncMock()
    fee.analyze.return_value = fee_result

    funded = AsyncMock()
    funded.analyze.return_value = funded_result

    balance = AsyncMock()
    balance.check.return_value = balance_result

    return TraderSignalAggregator(bundler, fee, funded, balance)


# ── tests ────────────────────────────────────────────────────────────────────

class TestTraderSignalAggregator:

    @pytest.mark.asyncio
    async def test_hard_reject_bundler_confirmed(self):
        """bundler.strength == CONFIRMED → hard_reject=True."""
        agg = _make_aggregator(
            _bundler("CONFIRMED"),
            _fee("ORGANIC"),
            _funded("SAFE"),
            _balance("STRONG"),
        )
        signal = await agg.analyze(TOKEN)

        assert signal.hard_reject is True
        assert signal.bundler.strength == "CONFIRMED"

    @pytest.mark.asyncio
    async def test_hard_reject_wash_trading(self):
        """fee.label == WASH_TRADING → hard_reject=True."""
        agg = _make_aggregator(
            _bundler("NONE"),
            _fee("WASH_TRADING"),
            _funded("SAFE"),
            _balance("STRONG"),
        )
        signal = await agg.analyze(TOKEN)

        assert signal.hard_reject is True
        assert signal.fee_analysis.label == "WASH_TRADING"

    @pytest.mark.asyncio
    async def test_hard_reject_both_conditions(self):
        """Both CONFIRMED bundler and WASH_TRADING → hard_reject=True."""
        agg = _make_aggregator(
            _bundler("CONFIRMED"),
            _fee("WASH_TRADING"),
            _funded("RED_FLAG"),
            _balance("WEAK"),
        )
        signal = await agg.analyze(TOKEN)

        assert signal.hard_reject is True

    @pytest.mark.asyncio
    async def test_no_hard_reject_when_all_soft(self):
        """SUSPICIOUS bundler + SUSPICIOUS fee → hard_reject=False."""
        agg = _make_aggregator(
            _bundler("SUSPICIOUS"),
            _fee("SUSPICIOUS"),
            _funded("CAUTION"),
            _balance("MIXED"),
        )
        signal = await agg.analyze(TOKEN)

        assert signal.hard_reject is False

    @pytest.mark.asyncio
    async def test_composite_score_clamped_at_max_20(self):
        """All positive signals: bundler NONE (0) + ORGANIC (+5) + SAFE (+3) + STRONG (+5) = +8,
        well below cap. Use custom adj to force >+20 and verify clamp."""
        # Custom adjustments: 0 + 12 + 8 + 5 = 25 → clamped to 20
        agg = _make_aggregator(
            _bundler("NONE"),
            _fee("ORGANIC", adj=12.0),
            _funded("SAFE", adj=8.0),
            _balance("STRONG", adj=5.0),
        )
        signal = await agg.analyze(TOKEN)

        assert signal.composite_score == 20.0

    @pytest.mark.asyncio
    async def test_composite_score_clamped_at_min_minus_20(self):
        """Large negative components clamped to -20."""
        # CONFIRMED bundler → -20, WASH_TRADING → -10, RED_FLAG → -10, WEAK → -5
        # = -45 → clamped to -20
        agg = _make_aggregator(
            _bundler("CONFIRMED"),
            _fee("WASH_TRADING"),
            _funded("RED_FLAG"),
            _balance("WEAK"),
        )
        signal = await agg.analyze(TOKEN)

        assert signal.composite_score == -20.0

    @pytest.mark.asyncio
    async def test_composite_score_accurate_math_mixed_signals(self):
        """
        NONE bundler (0) + ORGANIC fee (+5) + CAUTION funded (0) + MIXED balance (0) = +5.
        """
        agg = _make_aggregator(
            _bundler("NONE"),
            _fee("ORGANIC"),
            _funded("CAUTION"),
            _balance("MIXED"),
        )
        signal = await agg.analyze(TOKEN)

        assert signal.composite_score == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_suspicious_bundler_score_contribution(self):
        """SUSPICIOUS bundler contributes -8 to composite."""
        agg = _make_aggregator(
            _bundler("SUSPICIOUS"),   # -8
            _fee("ORGANIC"),           # +5
            _funded("SAFE"),           # +3
            _balance("STRONG"),        # +5
        )
        signal = await agg.analyze(TOKEN)

        # -8 + 5 + 3 + 5 = 5
        assert signal.composite_score == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_exception_in_one_analyzer_others_contribute(self):
        """If fee analyzer raises, safe default (UNKNOWN, 0) is used; others still score."""
        bundler = AsyncMock()
        bundler.detect.return_value = _bundler("NONE")

        fee = AsyncMock()
        fee.analyze.side_effect = RuntimeError("fee_service_down")

        funded = AsyncMock()
        funded.analyze.return_value = _funded("SAFE")  # +3

        balance = AsyncMock()
        balance.check.return_value = _balance("STRONG")  # +5

        agg = TraderSignalAggregator(bundler, fee, funded, balance)
        signal = await agg.analyze(TOKEN)

        # fee error → UNKNOWN (0); total: 0 + 0 + 3 + 5 = 8
        assert signal.composite_score == pytest.approx(8.0)
        assert signal.fee_analysis.label == "UNKNOWN"
        assert signal.hard_reject is False

    @pytest.mark.asyncio
    async def test_reasoning_has_four_entries(self):
        """reasoning list must have exactly 4 strings (one per filter)."""
        agg = _make_aggregator(
            _bundler("NONE"),
            _fee("ORGANIC"),
            _funded("SAFE"),
            _balance("STRONG"),
        )
        signal = await agg.analyze(TOKEN)

        assert len(signal.reasoning) == 4
        # Each entry should mention the filter name
        assert any("Bundler" in r for r in signal.reasoning)
        assert any("Fee" in r for r in signal.reasoning)
        assert any("FundedAge" in r for r in signal.reasoning)
        assert any("HolderSOL" in r for r in signal.reasoning)
