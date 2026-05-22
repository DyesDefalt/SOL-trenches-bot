"""Phase 11.1 + 11.3 tests — per-position overrides + rich card formatter."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.core.position import OpenPosition


# ---------------------------------------------------------------------------
# Phase 11.1: OpenPosition override fields + effective_*_pct logic
# ---------------------------------------------------------------------------


def _make_position(**overrides) -> OpenPosition:
    """Helper: build OpenPosition with sensible defaults + optional overrides."""
    defaults = {
        "db_id": 1,
        "token_address": "ADDR123",
        "token_symbol": "WIF",
        "entry_price_usd": 0.001,
        "entry_amount_sol": 0.02,
        "entry_amount_token": 20_000,
        "entry_timestamp": datetime.now(timezone.utc),
        "peak_price_usd": 0.001,
        "amount_remaining_token": 20_000,
    }
    defaults.update(overrides)
    return OpenPosition(**defaults)


def test_effective_tp_pct_default_when_no_override():
    pos = _make_position()
    assert pos.effective_tp1_pct(80.0) == 80.0
    assert pos.tp_override_pct is None


def test_effective_tp_pct_uses_override():
    pos = _make_position()
    pos.tp_override_pct = 25.0
    assert pos.effective_tp1_pct(80.0) == 25.0  # override beats default


def test_effective_sl_pct_default_when_no_override():
    pos = _make_position()
    assert pos.effective_sl_pct(-45.0) == -45.0


def test_effective_sl_pct_uses_override():
    pos = _make_position()
    pos.sl_override_pct = -15.0
    assert pos.effective_sl_pct(-45.0) == -15.0


def test_trail_disabled_default_false():
    pos = _make_position()
    assert pos.trail_disabled is False


def test_trail_disabled_can_be_toggled():
    pos = _make_position()
    pos.trail_disabled = True
    assert pos.trail_disabled is True


def test_phase11_metric_fields_default_none():
    """Extended metric fields default None so card formatter can skip them."""
    pos = _make_position()
    assert pos.current_price_usd is None
    assert pos.current_liquidity_usd is None
    assert pos.current_mcap_usd is None
    assert pos.buy_pressure_pct is None
    assert pos.vol_liq_ratio is None
    assert pos.rug_score is None


# ---------------------------------------------------------------------------
# Phase 11.3: format_position_card rendering
# ---------------------------------------------------------------------------


def test_format_position_card_minimal():
    """Card renders gracefully with only basic position dict (no metrics)."""
    from src.infra.telegram_menus import format_position_card
    pos = {
        "db_id": 1,
        "symbol": "WIF",
        "entry_price_usd": 0.001,
        "current_price_usd": 0.0015,
        "peak_price_usd": 0.002,
        "gain_pct": 50.0,
        "size_sol": 0.02,
        "tp_active_pct": 80.0,
        "sl_active_pct": -25.0,
        "trail_active": True,
    }
    out = format_position_card(pos)
    assert "WIF" in out
    assert "+50.00%" in out
    assert "0.02" in out  # size SOL
    assert "1.50x" in out  # current / entry multiple
    assert "2.00x" in out  # peak multiple
    assert "🟢" in out  # positive gain emoji


def test_format_position_card_negative_gain():
    from src.infra.telegram_menus import format_position_card
    pos = {
        "symbol": "RUG",
        "entry_price_usd": 0.001,
        "current_price_usd": 0.0008,
        "peak_price_usd": 0.001,
        "gain_pct": -20.0,
        "size_sol": 0.01,
        "tp_active_pct": 80.0,
        "sl_active_pct": -25.0,
        "trail_active": False,
    }
    out = format_position_card(pos)
    assert "-20.00%" in out
    assert "🔴" in out
    assert "○ OFF" in out  # trail disabled


def test_format_position_card_with_overrides_shows_OVR_tag():
    from src.infra.telegram_menus import format_position_card
    pos = {
        "symbol": "WIF",
        "entry_price_usd": 0.001,
        "current_price_usd": 0.0015,
        "peak_price_usd": 0.0015,
        "gain_pct": 50.0,
        "size_sol": 0.02,
        "tp_active_pct": 25.0,
        "sl_active_pct": -15.0,
        "trail_active": True,
        "tp_override": 25.0,
        "sl_override": -15.0,
    }
    out = format_position_card(pos)
    # OVR badges appear when override values are set
    assert out.count("[OVR]") == 2


def test_format_position_card_with_full_metrics():
    """When liq/mcap/rug/vol_liq/buy_pressure provided, they show up."""
    from src.infra.telegram_menus import format_position_card
    pos = {
        "symbol": "MOON",
        "entry_price_usd": 0.001,
        "current_price_usd": 0.0012,
        "peak_price_usd": 0.0015,
        "gain_pct": 20.0,
        "size_sol": 0.02,
        "tp_active_pct": 80.0,
        "sl_active_pct": -25.0,
        "trail_active": True,
        "liquidity_usd": 26500,
        "mcap_usd": 116300,
        "vol_liq_ratio": 3.5,
        "buy_pressure_pct": 52.5,
        "rug_score": 16,
    }
    out = format_position_card(pos)
    assert "Liq $26.5K" in out
    assert "MCap $116.3K" in out
    assert "V/L 3.5x" in out
    # Python's f"{52.5:.0f}" rounds to even → 52, not 53. Accept either.
    assert ("BuyP 53%" in out) or ("BuyP 52%" in out), out
    assert "Rug 16" in out


def test_format_position_card_tp_done_status():
    from src.infra.telegram_menus import format_position_card
    pos = {
        "symbol": "WIF",
        "entry_price_usd": 0.001,
        "current_price_usd": 0.002,
        "peak_price_usd": 0.0025,
        "gain_pct": 100.0,
        "size_sol": 0.02,
        "tp_active_pct": 80.0,
        "sl_active_pct": -25.0,
        "trail_active": True,
        "tp1_done": True,
        "tp2_done": False,
        "tp3_done": False,
    }
    out = format_position_card(pos)
    assert "TP1 done" in out


def test_format_position_card_handles_missing_price():
    """If current_price is missing, show loading state rather than crash."""
    from src.infra.telegram_menus import format_position_card
    pos = {
        "symbol": "PEND",
        "entry_price_usd": 0.001,
        "current_price_usd": None,
        "peak_price_usd": 0.001,
        "gain_pct": None,
        "size_sol": 0.02,
        "tp_active_pct": 80.0,
        "sl_active_pct": -25.0,
        "trail_active": True,
    }
    out = format_position_card(pos)
    assert "loading" in out.lower()


# ---------------------------------------------------------------------------
# Phase 11.1: PositionManager override methods (lightweight mock-based tests)
# ---------------------------------------------------------------------------


class _MockDB:
    """Captures override-related DB calls for assertion."""
    def __init__(self):
        self.calls: list[dict] = []

    async def update_position_override(self, position_id, **kwargs):
        self.calls.append({"position_id": position_id, **kwargs})


class _StubGecko:
    async def get_token(self, addr):
        return {"attributes": {"price_usd": 0.001}}


@pytest.mark.asyncio
async def test_override_tp_applies_to_in_memory_position():
    from src.core.position import PositionManager
    pm = PositionManager(db=_MockDB(), execution=object(), gecko=_StubGecko())  # type: ignore[arg-type]
    pos = _make_position(db_id=100)
    pm._positions[pos.token_address] = pos

    ok = await pm.override_tp(100, 25.0, set_by="test_user")
    assert ok is True
    assert pos.tp_override_pct == 25.0
    # DB call captured
    assert pm.db.calls[-1]["tp_pct"] == 25.0
    assert pm.db.calls[-1]["set_by"] == "test_user"


@pytest.mark.asyncio
async def test_override_sl_negative_value():
    from src.core.position import PositionManager
    pm = PositionManager(db=_MockDB(), execution=object(), gecko=_StubGecko())  # type: ignore[arg-type]
    pos = _make_position(db_id=101)
    pm._positions[pos.token_address] = pos

    ok = await pm.override_sl(101, -15.0)
    assert ok is True
    assert pos.sl_override_pct == -15.0


@pytest.mark.asyncio
async def test_override_returns_false_when_position_not_found():
    from src.core.position import PositionManager
    pm = PositionManager(db=_MockDB(), execution=object(), gecko=_StubGecko())  # type: ignore[arg-type]
    ok = await pm.override_tp(999, 50.0)
    assert ok is False


@pytest.mark.asyncio
async def test_toggle_trail_flips_state():
    from src.core.position import PositionManager
    pm = PositionManager(db=_MockDB(), execution=object(), gecko=_StubGecko())  # type: ignore[arg-type]
    pos = _make_position(db_id=200)
    assert pos.trail_disabled is False
    pm._positions[pos.token_address] = pos

    new_state = await pm.toggle_trail(200)
    assert new_state is True
    assert pos.trail_disabled is True

    # Toggle again
    new_state2 = await pm.toggle_trail(200)
    assert new_state2 is False
    assert pos.trail_disabled is False


@pytest.mark.asyncio
async def test_toggle_trail_returns_none_for_missing():
    from src.core.position import PositionManager
    pm = PositionManager(db=_MockDB(), execution=object(), gecko=_StubGecko())  # type: ignore[arg-type]
    result = await pm.toggle_trail(999)
    assert result is None


def test_get_open_positions_summary_includes_override_fields():
    """Phase 11.3: summary dict includes the keys the card formatter reads."""
    from src.core.position import PositionManager
    pm = PositionManager(db=_MockDB(), execution=object(), gecko=_StubGecko())  # type: ignore[arg-type]
    pos = _make_position(db_id=300)
    pos.tp_override_pct = 30.0
    pos.sl_override_pct = -20.0
    pos.trail_disabled = False
    pos.current_price_usd = 0.0015
    pos.current_liquidity_usd = 20_000
    pos.rug_score = 12
    pm._positions[pos.token_address] = pos

    summary = pm.get_open_positions_summary()
    assert len(summary) == 1
    s = summary[0]
    assert s["db_id"] == 300
    assert s["tp_active_pct"] == 30.0
    assert s["sl_active_pct"] == -20.0
    assert s["trail_active"] is True
    assert s["tp_override"] == 30.0
    assert s["sl_override"] == -20.0
    assert s["liquidity_usd"] == 20_000
    assert s["rug_score"] == 12
