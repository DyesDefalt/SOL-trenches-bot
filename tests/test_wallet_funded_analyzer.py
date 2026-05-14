"""
Tests for WalletFundedAnalyzer.

Coverage:
  1. RED_FLAG when median wallet age < 1 day
  2. CAUTION when median age 1-30 days
  3. SAFE when median age > 30 days
  4. UNKNOWN when no signatures returned
  5. Pagination: paginate to next page when first page full (100 sigs)
  6. Cap at 3 pages — stop even if more sigs exist
  7. top_holders argument respected (no Birdeye call)
  8. Birdeye fallback when top_holders not provided
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, call

import pytest

from src.intel.wallet_funded_analyzer import FundedFromAnalysis, WalletFundedAnalyzer, _median

TOKEN = "FundedTestToken333"
NOW = int(time.time())


def _sig(block_time: int, signature: str = "sig_xxx") -> dict:
    return {"blockTime": block_time, "signature": signature}


def _make_helius_with_age(addresses: list[str], age_seconds: float) -> AsyncMock:
    """
    Return Helius mock where each wallet has exactly one page of 1 signature
    whose blockTime is (now - age_seconds).
    """
    oldest_time = int(NOW - age_seconds)
    client = AsyncMock()
    client.get_signatures_for_address.return_value = [
        _sig(oldest_time, "sig_a"),
    ]
    return client


# ── unit tests: _median ──────────────────────────────────────────────────────

class TestMedianHelper:
    def test_single_value(self):
        assert _median([5.0]) == 5.0

    def test_even_count(self):
        assert _median([1.0, 3.0]) == 2.0

    def test_odd_count(self):
        assert _median([1.0, 2.0, 10.0]) == 2.0


# ── integration tests ────────────────────────────────────────────────────────

class TestWalletFundedAnalyzer:

    @pytest.mark.asyncio
    async def test_red_flag_when_all_wallets_fresh(self):
        """All wallets funded <1 day ago → RED_FLAG, score=-10."""
        holders = ["w1", "w2", "w3"]
        # 2 hours old
        helius = _make_helius_with_age(holders, 2 * 3600)

        analyzer = WalletFundedAnalyzer(helius)
        result = await analyzer.analyze(TOKEN, top_holders=holders)

        assert result.label == "RED_FLAG"
        assert result.score_adjustment == -10.0
        assert result.median_age_days is not None
        assert result.median_age_days < 1.0

    @pytest.mark.asyncio
    async def test_caution_when_wallets_are_days_old(self):
        """Wallets funded 5 days ago → CAUTION, score=0."""
        holders = ["w1", "w2"]
        helius = _make_helius_with_age(holders, 5 * 86_400)

        analyzer = WalletFundedAnalyzer(helius)
        result = await analyzer.analyze(TOKEN, top_holders=holders)

        assert result.label == "CAUTION"
        assert result.score_adjustment == 0.0

    @pytest.mark.asyncio
    async def test_safe_when_wallets_over_30_days(self):
        """Wallets funded 60 days ago → SAFE, score=+3."""
        holders = ["w1", "w2", "w3"]
        helius = _make_helius_with_age(holders, 60 * 86_400)

        analyzer = WalletFundedAnalyzer(helius)
        result = await analyzer.analyze(TOKEN, top_holders=holders)

        assert result.label == "SAFE"
        assert result.score_adjustment == 3.0
        assert result.median_age_days > 30.0

    @pytest.mark.asyncio
    async def test_unknown_when_no_signatures(self):
        """No signatures returned → UNKNOWN, score=0."""
        helius = AsyncMock()
        helius.get_signatures_for_address.return_value = []

        analyzer = WalletFundedAnalyzer(helius)
        result = await analyzer.analyze(TOKEN, top_holders=["w1"])

        assert result.label == "UNKNOWN"
        assert result.score_adjustment == 0.0
        assert result.median_age_days is None

    @pytest.mark.asyncio
    async def test_pagination_when_first_page_full(self):
        """If first page has 100 sigs, paginator fetches next page."""
        helius = AsyncMock()
        # Page 1: 100 sigs (full), last sig is "sig_100"
        page1 = [_sig(NOW - 100, f"sig_{i}") for i in range(100)]
        page1[-1]["signature"] = "sig_100"
        # Page 2: 1 sig (partial → end of history)
        page2 = [_sig(NOW - 86_400, "sig_101")]

        helius.get_signatures_for_address.side_effect = [page1, page2]

        analyzer = WalletFundedAnalyzer(helius)
        result = await analyzer.analyze(TOKEN, top_holders=["w1"])

        # Should have paginated; oldest blockTime = NOW-86400 ≈ 1 day
        assert helius.get_signatures_for_address.call_count == 2
        assert result.median_age_days is not None
        assert result.median_age_days > 0.9  # ~1 day

    @pytest.mark.asyncio
    async def test_cap_at_3_pages(self):
        """Pagination is capped at 3 pages even if history is longer."""
        helius = AsyncMock()
        # Always return 100 sigs (never reaches end)
        full_page = [_sig(NOW - i * 100, f"sig_{i}") for i in range(100)]
        full_page[-1]["signature"] = "sig_last"
        helius.get_signatures_for_address.return_value = full_page

        analyzer = WalletFundedAnalyzer(helius)
        await analyzer.analyze(TOKEN, top_holders=["w1"])

        # Should stop after 3 pages max
        assert helius.get_signatures_for_address.call_count == 3

    @pytest.mark.asyncio
    async def test_top_holders_respected_no_birdeye_call(self):
        """When top_holders provided, Birdeye is not called."""
        helius = _make_helius_with_age(["w1"], 5 * 86_400)
        birdeye = AsyncMock()

        analyzer = WalletFundedAnalyzer(helius, birdeye)
        await analyzer.analyze(TOKEN, top_holders=["w1"])

        birdeye.get_token_holders.assert_not_called()

    @pytest.mark.asyncio
    async def test_birdeye_fallback_when_no_top_holders(self):
        """When top_holders=None, fetch from Birdeye and proceed."""
        helius = _make_helius_with_age(["w1", "w2"], 60 * 86_400)
        birdeye = AsyncMock()
        birdeye.get_token_holders.return_value = [
            {"owner": "w1"},
            {"owner": "w2"},
        ]

        analyzer = WalletFundedAnalyzer(helius, birdeye)
        result = await analyzer.analyze(TOKEN, top_holders=None)

        birdeye.get_token_holders.assert_called_once()
        assert result.label == "SAFE"
