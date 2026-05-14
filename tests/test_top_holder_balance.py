"""
Tests for TopHolderBalanceChecker.

Coverage:
  1. STRONG when all holders have >0.5 SOL
  2. WEAK when 3+ holders have <0.2 SOL
  3. MIXED when mix of funded and weak wallets (but <3 weak)
  4. UNKNOWN when no holder addresses available
  5. Helius error for one wallet → treated as None (partial data handled)
  6. Cap at 8 holders
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.intel.top_holder_balance_check import HolderBalanceAnalysis, TopHolderBalanceChecker

TOKEN = "BalanceTestToken444"
_SOL = 1_000_000_000  # lamports per SOL


def _make_helius(balances: dict[str, int]) -> AsyncMock:
    """Helius mock: get_balance returns lamports from the dict."""
    client = AsyncMock()

    async def _bal(addr: str, **_: object) -> int:
        return balances.get(addr, 0)

    client.get_balance.side_effect = _bal
    return client


class TestTopHolderBalanceChecker:

    @pytest.mark.asyncio
    async def test_strong_when_all_holders_above_half_sol(self):
        """All holders >0.5 SOL → STRONG, score=+5."""
        holders = ["w1", "w2", "w3"]
        helius = _make_helius({
            "w1": 2 * _SOL,
            "w2": 1 * _SOL,
            "w3": int(0.6 * _SOL),
        })
        checker = TopHolderBalanceChecker(helius)
        result = await checker.check(TOKEN, top_holders=holders)

        assert result.label == "STRONG"
        assert result.score_adjustment == 5.0
        assert result.weak_count == 0
        assert result.min_balance_sol is not None
        assert result.min_balance_sol > 0.5

    @pytest.mark.asyncio
    async def test_weak_when_3_or_more_holders_below_threshold(self):
        """3 holders with <0.2 SOL → WEAK, score=-5."""
        holders = ["w1", "w2", "w3", "w4"]
        helius = _make_helius({
            "w1": int(0.05 * _SOL),  # weak
            "w2": int(0.1 * _SOL),   # weak
            "w3": int(0.15 * _SOL),  # weak
            "w4": 3 * _SOL,           # strong
        })
        checker = TopHolderBalanceChecker(helius)
        result = await checker.check(TOKEN, top_holders=holders)

        assert result.label == "WEAK"
        assert result.score_adjustment == -5.0
        assert result.weak_count == 3

    @pytest.mark.asyncio
    async def test_mixed_when_some_weak_but_below_threshold(self):
        """2 weak holders (<3) and 2 strong → MIXED, score=0."""
        holders = ["w1", "w2", "w3", "w4"]
        helius = _make_helius({
            "w1": int(0.05 * _SOL),  # weak
            "w2": int(0.1 * _SOL),   # weak
            "w3": 1 * _SOL,
            "w4": 2 * _SOL,
        })
        checker = TopHolderBalanceChecker(helius)
        result = await checker.check(TOKEN, top_holders=holders)

        assert result.label == "MIXED"
        assert result.score_adjustment == 0.0
        assert result.weak_count == 2

    @pytest.mark.asyncio
    async def test_unknown_when_no_holders_available(self):
        """No holder addresses → UNKNOWN, score=0."""
        helius = _make_helius({})
        checker = TopHolderBalanceChecker(helius, birdeye=None)
        result = await checker.check(TOKEN, top_holders=None)

        assert result.label == "UNKNOWN"
        assert result.score_adjustment == 0.0
        assert result.min_balance_sol is None

    @pytest.mark.asyncio
    async def test_helius_error_for_one_wallet_handled_gracefully(self):
        """If one wallet's getBalance fails, it's skipped (None); others still assessed."""
        helius = AsyncMock()

        call_count = 0

        async def _bal(addr: str, **_: object) -> int:
            nonlocal call_count
            call_count += 1
            if addr == "w_bad":
                raise RuntimeError("rpc_error")
            return 2 * _SOL  # all good wallets have 2 SOL

        helius.get_balance.side_effect = _bal

        checker = TopHolderBalanceChecker(helius)
        result = await checker.check(TOKEN, top_holders=["w1", "w_bad", "w3"])

        # Should still return STRONG from the 2 valid wallets
        assert result.label == "STRONG"
        assert result.weak_count == 0

    @pytest.mark.asyncio
    async def test_cap_at_8_holders(self):
        """Only first 8 holders are sampled regardless of list length."""
        holders = [f"w{i}" for i in range(15)]
        balances = {f"w{i}": _SOL for i in range(15)}
        helius = _make_helius(balances)

        checker = TopHolderBalanceChecker(helius)
        await checker.check(TOKEN, top_holders=holders)

        assert helius.get_balance.call_count == 8
