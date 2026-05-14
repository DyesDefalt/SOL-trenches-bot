"""
Tests for BundlerPatternDetector.

Coverage:
  1. CONFIRMED when 3+ holders share supply_pct and SOL balance
  2. SUSPICIOUS when exactly 2 holders match
  3. NONE when no pairwise similarity exists
  4. Graceful degrade: Birdeye None → returns NONE strength
  5. Graceful degrade: Birdeye raises → returns NONE strength
  6. Holders with percentage field directly (no ui_amount normalisation)
  7. Supply pct outside tolerance → not clustered
  8. SOL balance outside tolerance → not clustered despite supply match
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.intel.bundler_pattern_detector import BundlerPattern, BundlerPatternDetector

TOKEN = "BundlerTestToken111"


def _make_birdeye(holders: list[dict]) -> MagicMock:
    """Return a Birdeye mock that yields the given holders list."""
    client = AsyncMock()
    client.get_token_holders.return_value = holders
    return client


def _make_helius(balances: dict[str, int]) -> MagicMock:
    """
    Return a Helius mock where get_balance returns lamports per address.
    balances: {address: lamports}
    """
    client = AsyncMock()

    async def _bal(addr: str, **_kwargs: object) -> int:
        return balances.get(addr, 500_000_000)  # default 0.5 SOL

    client.get_balance.side_effect = _bal
    return client


# ── fixture helpers ──────────────────────────────────────────────────────────

def _holder(owner: str, pct: float) -> dict:
    """Holder with direct 'percentage' field."""
    return {"owner": owner, "percentage": pct}


def _holder_ui(owner: str, ui: float) -> dict:
    """Holder with ui_amount field (requires normalisation)."""
    return {"owner": owner, "ui_amount": ui}


# ── tests ────────────────────────────────────────────────────────────────────

class TestBundlerPatternDetector:

    @pytest.mark.asyncio
    async def test_confirmed_when_3_holders_share_supply_and_sol(self):
        """3 holders with same supply% and similar SOL → CONFIRMED."""
        holders = [
            _holder("wallet_a", 10.0),
            _holder("wallet_b", 10.5),  # within ±5% of 10.0
            _holder("wallet_c", 9.8),   # within ±5% of 10.0
            _holder("wallet_d", 50.0),  # outlier — will not cluster
        ]
        # All 3 matching wallets have ~1.0 SOL (within ±20%)
        helius = _make_helius({
            "wallet_a": 1_000_000_000,
            "wallet_b": 950_000_000,
            "wallet_c": 1_050_000_000,
            "wallet_d": 5_000_000_000,
        })
        birdeye = _make_birdeye(holders)

        detector = BundlerPatternDetector(birdeye, helius)
        result = await detector.detect(TOKEN)

        assert result.strength == "CONFIRMED"
        assert len(result.detected_wallets) >= 3
        assert result.total_supply_pct > 0

    @pytest.mark.asyncio
    async def test_suspicious_when_exactly_2_holders_match(self):
        """Exactly 2 holders match supply% AND SOL → SUSPICIOUS."""
        holders = [
            _holder("wallet_x", 10.0),
            _holder("wallet_y", 10.3),   # within ±5%
            _holder("wallet_z", 35.0),   # far off
            _holder("wallet_w", 40.0),   # far off
        ]
        helius = _make_helius({
            "wallet_x": 1_000_000_000,
            "wallet_y": 900_000_000,    # ~±10% of x, within ±20%
            "wallet_z": 8_000_000_000,
            "wallet_w": 9_000_000_000,
        })
        birdeye = _make_birdeye(holders)

        detector = BundlerPatternDetector(birdeye, helius)
        result = await detector.detect(TOKEN)

        assert result.strength == "SUSPICIOUS"
        assert len(result.detected_wallets) == 2

    @pytest.mark.asyncio
    async def test_none_when_no_holders_are_similar(self):
        """All holders have different supply% → NONE."""
        holders = [
            _holder("w1", 5.0),
            _holder("w2", 20.0),
            _holder("w3", 40.0),
            _holder("w4", 70.0),
        ]
        helius = _make_helius({})
        birdeye = _make_birdeye(holders)

        detector = BundlerPatternDetector(birdeye, helius)
        result = await detector.detect(TOKEN)

        assert result.strength == "NONE"
        assert result.detected_wallets == []

    @pytest.mark.asyncio
    async def test_graceful_degrade_birdeye_none(self):
        """If birdeye=None, return NONE without crashing."""
        detector = BundlerPatternDetector(birdeye=None, helius_rpc=None)
        result = await detector.detect(TOKEN)

        assert result.strength == "NONE"
        assert "No holder data" in result.reasoning

    @pytest.mark.asyncio
    async def test_graceful_degrade_birdeye_raises(self):
        """If Birdeye raises, return NONE strength."""
        birdeye = AsyncMock()
        birdeye.get_token_holders.side_effect = Exception("birdeye_timeout")

        detector = BundlerPatternDetector(birdeye=birdeye, helius_rpc=None)
        result = await detector.detect(TOKEN)

        assert result.strength == "NONE"

    @pytest.mark.asyncio
    async def test_supply_pct_outside_tolerance_not_clustered(self):
        """Holders with >5% supply difference are NOT clustered even with same SOL."""
        holders = [
            _holder("wa", 10.0),
            _holder("wb", 16.0),  # diff = 6.0% > 5% tolerance → no cluster
        ]
        helius = _make_helius({
            "wa": 1_000_000_000,
            "wb": 1_000_000_000,  # identical SOL
        })
        birdeye = _make_birdeye(holders)

        detector = BundlerPatternDetector(birdeye, helius)
        result = await detector.detect(TOKEN)

        assert result.strength == "NONE"

    @pytest.mark.asyncio
    async def test_sol_balance_outside_tolerance_not_clustered(self):
        """Holders match on supply% but have very different SOL → NOT clustered."""
        holders = [
            _holder("wa", 10.0),
            _holder("wb", 10.2),  # supply within ±5%
        ]
        helius = _make_helius({
            "wa": 100_000_000,    # 0.1 SOL
            "wb": 5_000_000_000,  # 5 SOL — way more, >20% relative diff
        })
        birdeye = _make_birdeye(holders)

        detector = BundlerPatternDetector(birdeye, helius)
        result = await detector.detect(TOKEN)

        assert result.strength == "NONE"

    @pytest.mark.asyncio
    async def test_normalise_ui_amount_holders(self):
        """Holders without percentage field (ui_amount) are normalised correctly."""
        # ui_amount: 1000, 1050, 500, 3000
        # Total = 5550; pct: ~18%, ~18.9%, ~9%, ~54%
        # wallet_a and wallet_b will be within ±5% of each other (≈18%)
        holders = [
            _holder_ui("wallet_a", 1000.0),
            _holder_ui("wallet_b", 1050.0),
            _holder_ui("wallet_c", 500.0),
            _holder_ui("wallet_d", 3000.0),
        ]
        helius = _make_helius({
            "wallet_a": 1_000_000_000,
            "wallet_b": 1_000_000_000,
        })
        birdeye = _make_birdeye(holders)

        detector = BundlerPatternDetector(birdeye, helius)
        result = await detector.detect(TOKEN)

        # wallet_a (~18%) and wallet_b (~18.9%) should pair as SUSPICIOUS (only 2)
        assert result.strength in {"SUSPICIOUS", "CONFIRMED"}
