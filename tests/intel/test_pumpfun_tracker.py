"""
Tests untuk PumpfunTracker — Phase 7e.

Coverage:
- Sweet spot detection (70-95%) → score_bonus = +10
- Graduated token → score_bonus = -5
- Non-pumpfun token (None dari client) → is_pumpfun=False, score_bonus=0
- Early token (<30%) → score_bonus = 0
- 50-70% range → score_bonus = +5
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.intel.pumpfun_tracker import GraduationStatus, PumpfunTracker, _compute_score_bonus


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

def _make_pumpfun_client(
    token_info: dict | None = None,
    graduation_pct: float = 0.0,
    is_graduated: bool = False,
    is_in_sweet_spot: bool = False,
) -> AsyncMock:
    """Helper to create a mocked PumpfunClient."""
    client = AsyncMock()
    client.get_token_info = AsyncMock(return_value=token_info)
    client.graduation_progress_pct = MagicMock(return_value=graduation_pct)
    client.is_graduated = MagicMock(return_value=is_graduated)
    client.is_in_sweet_spot = MagicMock(return_value=is_in_sweet_spot)
    return client


TOKEN = "PuMpFuNtOkEnMiNt1111111111111111111"


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sweet_spot_gives_bonus():
    """Token di 70-95% bonding curve → is_in_sweet_spot=True, score_bonus=+10."""
    token_info = {"usd_market_cap": 45_000, "bonding_curve_progress": 82.0}
    client = _make_pumpfun_client(
        token_info=token_info,
        graduation_pct=82.0,
        is_graduated=False,
        is_in_sweet_spot=True,
    )
    tracker = PumpfunTracker(client)
    status = await tracker.check(TOKEN)

    assert status.is_pumpfun is True
    assert status.is_in_sweet_spot is True
    assert status.is_graduated is False
    assert status.score_bonus == pytest.approx(10.0)
    assert status.graduation_pct == pytest.approx(82.0)


@pytest.mark.asyncio
async def test_graduated_token_gets_penalty():
    """Token sudah graduated ke Raydium → score_bonus = -5 (sudah pumped)."""
    token_info = {"usd_market_cap": 100_000, "bonding_curve_progress": 100.0}
    client = _make_pumpfun_client(
        token_info=token_info,
        graduation_pct=100.0,
        is_graduated=True,
        is_in_sweet_spot=False,
    )
    tracker = PumpfunTracker(client)
    status = await tracker.check(TOKEN)

    assert status.is_pumpfun is True
    assert status.is_graduated is True
    assert status.score_bonus == pytest.approx(-5.0)


@pytest.mark.asyncio
async def test_non_pumpfun_token_no_bonus():
    """Token bukan Pump.fun (client returns None) → is_pumpfun=False, score_bonus=0."""
    client = _make_pumpfun_client(token_info=None)
    tracker = PumpfunTracker(client)
    status = await tracker.check(TOKEN)

    assert status.is_pumpfun is False
    assert status.score_bonus == pytest.approx(0.0)
    assert status.graduation_pct == pytest.approx(0.0)
    assert status.market_cap_usd == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_early_token_no_bonus():
    """Token di <30% bonding curve → terlalu awal, score_bonus=0."""
    token_info = {"usd_market_cap": 8_000, "bonding_curve_progress": 15.0}
    client = _make_pumpfun_client(
        token_info=token_info,
        graduation_pct=15.0,
        is_graduated=False,
        is_in_sweet_spot=False,
    )
    tracker = PumpfunTracker(client)
    status = await tracker.check(TOKEN)

    assert status.is_pumpfun is True
    assert status.score_bonus == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_mid_range_token_gets_small_bonus():
    """Token di 55% bonding curve → score_bonus=+5."""
    token_info = {"usd_market_cap": 25_000, "bonding_curve_progress": 55.0}
    client = _make_pumpfun_client(
        token_info=token_info,
        graduation_pct=55.0,
        is_graduated=False,
        is_in_sweet_spot=False,
    )
    tracker = PumpfunTracker(client)
    status = await tracker.check(TOKEN)

    assert status.is_pumpfun is True
    assert status.score_bonus == pytest.approx(5.0)


# --------------------------------------------------------------------------
# Unit tests untuk _compute_score_bonus helper
# --------------------------------------------------------------------------

def test_compute_bonus_all_ranges():
    """Verifikasi bonus untuk semua range graduation pct."""
    def make(pct: float, graduated: bool = False, is_pumpfun: bool = True) -> GraduationStatus:
        return GraduationStatus(
            token_address=TOKEN,
            is_pumpfun=is_pumpfun,
            graduation_pct=pct,
            is_graduated=graduated,
        )

    # Not pumpfun
    assert _compute_score_bonus(make(80.0, is_pumpfun=False)) == 0.0
    # Graduated
    assert _compute_score_bonus(make(100.0, graduated=True)) == -5.0
    # Sweet spot
    assert _compute_score_bonus(make(80.0)) == 10.0
    assert _compute_score_bonus(make(70.0)) == 10.0
    assert _compute_score_bonus(make(95.0)) == 10.0
    # 50-70
    assert _compute_score_bonus(make(60.0)) == 5.0
    # 30-50
    assert _compute_score_bonus(make(40.0)) == 2.0
    # Early
    assert _compute_score_bonus(make(10.0)) == 0.0
    # >95 not graduated
    assert _compute_score_bonus(make(97.0)) == 0.0
