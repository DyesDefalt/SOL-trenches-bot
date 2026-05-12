"""Unit tests untuk SmartWalletRegistry — discovery, classification, persistence."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.core.smart_wallet_registry import (
    SmartWallet,
    SmartWalletRegistry,
    _classify_tier_from_stats,
)


# ----------------------------------------------------------------------
# Tier classification
# ----------------------------------------------------------------------
def test_classify_a_tier() -> None:
    """winrate ≥ 65% AND realized_profit ≥ 30 SOL → A."""
    tier = _classify_tier_from_stats(
        winrate=0.70, realized_profit=50, min_trades=30, buy_count=20, sell_count=20
    )
    assert tier == "A"


def test_classify_b_tier() -> None:
    """winrate 55-64% → B."""
    tier = _classify_tier_from_stats(
        winrate=0.60, realized_profit=10, min_trades=30, buy_count=15, sell_count=20
    )
    assert tier == "B"


def test_classify_c_tier() -> None:
    """winrate 45-54% → C."""
    tier = _classify_tier_from_stats(
        winrate=0.50, realized_profit=5, min_trades=30, buy_count=15, sell_count=20
    )
    assert tier == "C"


def test_classify_f_low_winrate() -> None:
    """winrate < 45% → F."""
    tier = _classify_tier_from_stats(
        winrate=0.30, realized_profit=10, min_trades=30, buy_count=15, sell_count=20
    )
    assert tier == "F"


def test_classify_f_insufficient_trades() -> None:
    """Trade count < min_trades → F (regardless of winrate)."""
    tier = _classify_tier_from_stats(
        winrate=0.90, realized_profit=100, min_trades=30, buy_count=5, sell_count=5
    )
    assert tier == "F"


def test_classify_a_requires_both_winrate_and_profit() -> None:
    """High winrate tapi low profit → B (not A)."""
    tier = _classify_tier_from_stats(
        winrate=0.80, realized_profit=10, min_trades=30, buy_count=20, sell_count=20
    )
    # winrate=0.80 + profit=10 (kurang 30) → bukan A. Falls to B (winrate >= 0.55)
    assert tier == "B"


# ----------------------------------------------------------------------
# SmartWallet dataclass
# ----------------------------------------------------------------------
def test_smart_wallet_active_check() -> None:
    sw = SmartWallet(address="abc", tier="A")
    assert sw.is_active is True
    assert sw.is_top_tier is True
    assert sw.score_multiplier == 1.0


def test_smart_wallet_blacklist_inactive() -> None:
    sw = SmartWallet(address="abc", tier="BLACKLIST")
    assert sw.is_active is False
    assert sw.is_top_tier is False
    assert sw.score_multiplier == 0.0


def test_smart_wallet_c_tier_active_but_not_top() -> None:
    sw = SmartWallet(address="abc", tier="C")
    assert sw.is_active is True
    assert sw.is_top_tier is False
    assert sw.score_multiplier == 0.4


def test_smart_wallet_manual_a_treated_as_top_tier() -> None:
    sw = SmartWallet(address="abc", tier="MANUAL_A")
    assert sw.is_active is True
    assert sw.is_top_tier is True
    assert sw.score_multiplier == 1.0


# ----------------------------------------------------------------------
# Registry persistence
# ----------------------------------------------------------------------
@pytest.fixture
def tmp_registry(tmp_path: Path) -> SmartWalletRegistry:
    return SmartWalletRegistry(
        registry_path=tmp_path / "smart_wallets.json",
        manual_path=tmp_path / "manual.json",
        blacklist_path=tmp_path / "blacklist.json",
    )


@pytest.mark.asyncio
async def test_load_empty_registry(tmp_registry: SmartWalletRegistry) -> None:
    count = await tmp_registry.load()
    assert count == 0
    assert tmp_registry.stats_summary() == {}


@pytest.mark.asyncio
async def test_save_and_load_roundtrip(tmp_registry: SmartWalletRegistry) -> None:
    # Manually populate
    sw = SmartWallet(
        address="WaLLeT123",
        tier="A",
        winrate=0.75,
        realized_profit=50.0,
        buy_count=30,
        sell_count=25,
        source="auto",
    )
    tmp_registry._wallets[sw.address.lower()] = sw

    await tmp_registry.save()
    assert tmp_registry.registry_path.exists()

    # Re-load di registry baru
    new_registry = SmartWalletRegistry(
        registry_path=tmp_registry.registry_path,
        manual_path=tmp_registry.manual_path,
        blacklist_path=tmp_registry.blacklist_path,
    )
    count = await new_registry.load()
    assert count == 1

    loaded = new_registry.get_by_address("WaLLeT123")
    assert loaded is not None
    assert loaded.tier == "A"
    assert loaded.winrate == 0.75


@pytest.mark.asyncio
async def test_add_manual_overrides_auto(tmp_registry: SmartWalletRegistry) -> None:
    """Manual addition harus override auto classification."""
    # Auto: F-tier
    auto_sw = SmartWallet(address="WaLLeT", tier="F", source="auto")
    tmp_registry._wallets[auto_sw.address.lower()] = auto_sw

    # Manual: A-tier — harus override
    tmp_registry.add_manual(address="WaLLeT", tier="A", notes="alpha trader")

    sw = tmp_registry.get_by_address("WaLLeT")
    assert sw is not None
    assert sw.tier == "MANUAL_A"
    assert sw.source == "manual"
    assert sw.notes == "alpha trader"


@pytest.mark.asyncio
async def test_blacklist_overrides_everything(tmp_registry: SmartWalletRegistry) -> None:
    """Blacklist override semua, termasuk manual."""
    tmp_registry.add_manual(address="WaLLeT", tier="A", notes="initially good")
    tmp_registry.add_blacklist(address="WaLLeT", notes="turned out wash trader")

    sw = tmp_registry.get_by_address("WaLLeT")
    assert sw is not None
    assert sw.tier == "BLACKLIST"
    assert sw.is_active is False


@pytest.mark.asyncio
async def test_get_top_tier_sorts_a_first(tmp_registry: SmartWalletRegistry) -> None:
    """Top tier query: A first, lalu B, sort by winrate desc."""
    tmp_registry._wallets = {
        "b1": SmartWallet(address="B1", tier="B", winrate=0.60, source="auto"),
        "a1": SmartWallet(address="A1", tier="A", winrate=0.70, source="auto"),
        "a2": SmartWallet(address="A2", tier="A", winrate=0.80, source="auto"),
        "c1": SmartWallet(address="C1", tier="C", winrate=0.50, source="auto"),
    }

    top = tmp_registry.get_top_tier_wallets()
    assert len(top) == 3  # C tier excluded
    assert top[0].tier in ("A", "MANUAL_A")
    assert top[0].address == "A2"  # higher winrate
    assert top[1].address == "A1"
    assert top[2].address == "B1"


@pytest.mark.asyncio
async def test_top_tier_max_count(tmp_registry: SmartWalletRegistry) -> None:
    for i in range(20):
        addr = f"WALLET_{i}"
        tmp_registry._wallets[addr.lower()] = SmartWallet(
            address=addr, tier="A", winrate=0.7, source="auto"
        )
    top = tmp_registry.get_top_tier_wallets(max_count=10)
    assert len(top) == 10


@pytest.mark.asyncio
async def test_is_smart_wallet_check(tmp_registry: SmartWalletRegistry) -> None:
    tmp_registry._wallets["abc"] = SmartWallet(address="ABC", tier="A", source="auto")
    tmp_registry._wallets["def"] = SmartWallet(address="DEF", tier="F", source="auto")

    assert tmp_registry.is_smart_wallet("ABC") is True
    assert tmp_registry.is_smart_wallet("abc") is True  # case-insensitive
    assert tmp_registry.is_smart_wallet("DEF") is False
    assert tmp_registry.is_smart_wallet("XYZ") is False  # not registered


# ----------------------------------------------------------------------
# Bootstrap (integration with mocked GMGN)
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bootstrap_classifies_candidates(tmp_registry: SmartWalletRegistry) -> None:
    mock_gmgn = AsyncMock()

    # Mock smart money trades — 2 unique wallets
    mock_gmgn.get_smart_money_trades.return_value = [
        {"maker_info": {"address": "AAA111"}, "base_address": "tok1"},
        {"maker_info": {"address": "BBB222"}, "base_address": "tok2"},
    ]
    mock_gmgn.get_kol_trades.return_value = []

    # Mock stats responses
    async def mock_stats(wallet: str, **kwargs):
        if wallet == "AAA111":
            return {
                "winrate": 0.75,
                "realized_profit": 50,
                "total_profit": 80,
                "buy_count": 50,
                "sell_count": 45,
                "token_num": 30,
            }
        elif wallet == "BBB222":
            return {
                "winrate": 0.60,
                "realized_profit": 15,
                "total_profit": 20,
                "buy_count": 25,
                "sell_count": 20,
                "token_num": 15,
            }
        return {}

    mock_gmgn.get_wallet_stats.side_effect = mock_stats

    result = await tmp_registry.bootstrap_from_gmgn(mock_gmgn, sample_size=10)

    # AAA111: winrate 0.75 + profit 50 → A
    # BBB222: winrate 0.60 + profit 15 → B
    assert result["A"] == 1
    assert result["B"] == 1

    a_wallet = tmp_registry.get_by_address("AAA111")
    assert a_wallet is not None
    assert a_wallet.tier == "A"
    assert a_wallet.winrate == 0.75


@pytest.mark.asyncio
async def test_bootstrap_skips_manual_wallets(tmp_registry: SmartWalletRegistry) -> None:
    """Manual wallets tidak boleh di-override saat bootstrap."""
    tmp_registry.add_manual(address="MANUAL1", tier="A", notes="hand-curated")

    mock_gmgn = AsyncMock()
    mock_gmgn.get_smart_money_trades.return_value = [
        {"maker_info": {"address": "MANUAL1"}, "base_address": "tok1"},
    ]
    mock_gmgn.get_kol_trades.return_value = []

    # Stats yang akan kembalikan tier F
    mock_gmgn.get_wallet_stats.return_value = {
        "winrate": 0.20, "realized_profit": 0, "buy_count": 5, "sell_count": 5
    }

    await tmp_registry.bootstrap_from_gmgn(mock_gmgn, sample_size=5)

    # Manual harus tetap A
    sw = tmp_registry.get_by_address("MANUAL1")
    assert sw is not None
    assert sw.tier == "MANUAL_A"
    assert sw.notes == "hand-curated"
