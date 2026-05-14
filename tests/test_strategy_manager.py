"""
Unit tests for StrategyManager (Phase 10).

No live Postgres required — all DB interactions are mocked via a fake
Database object whose pool is a MagicMock/AsyncMock.

Pattern mirrors tests/test_smart_wallet_registry.py and the AsyncMock
usage in tests/ai/test_reflection_agent.py.
"""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.strategy_manager import StrategyManager, _build_env_fallback, _coerce_value


# ---------------------------------------------------------------------------
# Helpers — fake DB and fake asyncpg rows
# ---------------------------------------------------------------------------

def _make_record(data: dict[str, Any]) -> MagicMock:
    """
    Produce an object that behaves like an asyncpg Record:
    supports dict(record) and record["key"] access.
    """
    record = MagicMock()
    record.__getitem__ = lambda self, k: data[k]
    record.keys = lambda: data.keys()
    record.__iter__ = lambda self: iter(data.keys())
    # Make dict(record) work by implementing items()
    record.items = lambda: data.items()
    # asyncpg Records also support direct attribute-style access for dict()
    # We monkey-patch __class__ to make dict() work via MagicMock
    record._data = data
    # Override so that dict(record) returns data keys correctly
    record.__class__ = type(
        "FakeRecord",
        (),
        {
            "__getitem__": lambda s, k: data[k],
            "keys": lambda s: data.keys(),
            "items": lambda s: data.items(),
            "__iter__": lambda s: iter(data.keys()),
        },
    )
    return record


def _make_row(
    id: str = "balanced",
    name: str = "Balanced",
    enabled: bool = True,
    config: dict | None = None,
) -> Any:
    """Build a fake asyncpg Record for a strategy row."""
    if config is None:
        config = {
            "min_score_to_buy": 75,
            "max_position_size_sol": 0.05,
            "max_concurrent_positions": 2,
            "filter_max_mcap_usd": 60000,
            "filter_min_liquidity_usd": 8000,
            "filter_min_gmgn_security_score": 70,
            "tp1_gain_pct": 80,
            "tp1_sell_pct": 30,
            "tp2_gain_pct": 150,
            "tp2_sell_pct": 30,
            "tp3_gain_pct": 300,
            "tp3_sell_pct": 25,
            "hard_sl_pct": -45,
            "trailing_stop_pct": 30,
            "time_based_exit_minutes": 45,
            "slippage_bps": 1500,
            "entry_mode": "immediate",
            "max_ath_distance_pct": -10,
            "score_weight_smart_money": 35,
            "score_weight_security": 10,
        }
    return _make_record({"id": id, "name": name, "enabled": enabled, "config": config})


def _make_fake_db(fetchrow_return: Any = None, fetch_return: list | None = None) -> MagicMock:
    """
    Build a fake Database object whose pool mimics asyncpg connection
    context-manager pattern: pool.acquire() → async context → conn.
    """
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    conn.fetch = AsyncMock(return_value=fetch_return or [])
    conn.fetchval = AsyncMock(return_value=1)  # strategy exists by default
    # execute returns a command tag string like "UPDATE 1"
    conn.execute = AsyncMock(return_value="UPDATE 1")
    # transaction() must be an async context manager
    txn_cm = AsyncMock()
    txn_cm.__aenter__ = AsyncMock(return_value=txn_cm)
    txn_cm.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn_cm)

    acquire_cm = AsyncMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_cm)

    db = MagicMock()
    db._pool = pool
    db._conn = conn  # shortcut for assertions
    return db


# ---------------------------------------------------------------------------
# 1. get_active — happy path: returns DB row
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_active_returns_db_config() -> None:
    row = _make_row()
    db = _make_fake_db(fetchrow_return=row)
    sm = StrategyManager(db)

    config = await sm.get_active()

    assert config["min_score_to_buy"] == 75
    assert config["entry_mode"] == "immediate"


# ---------------------------------------------------------------------------
# 2. get_active — fallback to env when DB returns None
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_active_fallback_when_no_active_row() -> None:
    db = _make_fake_db(fetchrow_return=None)
    sm = StrategyManager(db)

    env_cfg = _build_env_fallback()
    config = await sm.get_active()

    # Should match env fallback structure
    assert "min_score_to_buy" in config
    assert config["min_score_to_buy"] == env_cfg["min_score_to_buy"]


# ---------------------------------------------------------------------------
# 3. get_active — fallback to env when DB raises an exception
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_active_fallback_on_db_error() -> None:
    db = MagicMock()
    db._pool = MagicMock()
    acquire_cm = AsyncMock()
    acquire_cm.__aenter__ = AsyncMock(side_effect=OSError("connection refused"))
    db._pool.acquire = MagicMock(return_value=acquire_cm)

    sm = StrategyManager(db)
    config = await sm.get_active()

    # Must return env fallback without raising
    assert "min_score_to_buy" in config


# ---------------------------------------------------------------------------
# 4. Cache TTL — second call within TTL returns cached value
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_active_cache_hit_no_extra_db_call() -> None:
    row = _make_row()
    db = _make_fake_db(fetchrow_return=row)
    sm = StrategyManager(db)

    await sm.get_active()
    await sm.get_active()  # should hit cache

    # DB conn.fetchrow should have been called exactly once
    db._conn.fetchrow.assert_called_once()


# ---------------------------------------------------------------------------
# 5. Cache TTL — cache expires after TTL, DB re-fetched
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_active_cache_expires_after_ttl() -> None:
    row = _make_row()
    db = _make_fake_db(fetchrow_return=row)
    sm = StrategyManager(db)
    sm._ttl = 0.05  # 50 ms for test speed

    await sm.get_active()
    # Wait for TTL to expire
    time.sleep(0.1)
    await sm.get_active()

    assert db._conn.fetchrow.call_count == 2


# ---------------------------------------------------------------------------
# 6. invalidate_cache — forces re-fetch on next call
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_invalidate_cache_forces_refetch() -> None:
    row = _make_row()
    db = _make_fake_db(fetchrow_return=row)
    sm = StrategyManager(db)

    await sm.get_active()
    sm.invalidate_cache()
    await sm.get_active()

    assert db._conn.fetchrow.call_count == 2


# ---------------------------------------------------------------------------
# 7. list_all — returns id / name / enabled for all rows
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_list_all_returns_all_strategies() -> None:
    rows = [
        _make_record({"id": "conservative", "name": "Conservative", "enabled": False}),
        _make_record({"id": "balanced", "name": "Balanced", "enabled": True}),
        _make_record({"id": "aggressive", "name": "Aggressive", "enabled": False}),
        _make_record({"id": "dip_buy", "name": "Dip Buy", "enabled": False}),
    ]
    db = _make_fake_db(fetch_return=rows)
    sm = StrategyManager(db)

    result = await sm.list_all()

    assert len(result) == 4
    ids = [r["id"] for r in result]
    assert "balanced" in ids
    assert "dip_buy" in ids


# ---------------------------------------------------------------------------
# 8. get_by_id — returns strategy dict for valid id
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_by_id_returns_strategy() -> None:
    row = _make_row(id="conservative", name="Conservative", enabled=False)
    db = _make_fake_db(fetchrow_return=row)
    sm = StrategyManager(db)

    result = await sm.get_by_id("conservative")

    assert result is not None
    assert result["id"] == "conservative"
    assert result["name"] == "Conservative"
    assert isinstance(result["config"], dict)


# ---------------------------------------------------------------------------
# 9. get_by_id — returns None for unknown id
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_by_id_returns_none_for_unknown_id() -> None:
    db = _make_fake_db(fetchrow_return=None)
    sm = StrategyManager(db)

    result = await sm.get_by_id("nonexistent_strategy_xyz")

    assert result is None


# ---------------------------------------------------------------------------
# 10. set_active — disables all others, enables target, invalidates cache
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_set_active_toggles_enabled_flag() -> None:
    db = _make_fake_db()
    sm = StrategyManager(db)

    # Seed cache so we can verify it gets invalidated
    sm._cache = {"min_score_to_buy": 75}
    sm._cache_at = time.monotonic()

    success = await sm.set_active("aggressive")

    assert success is True
    # Cache must be busted
    assert sm._cache is None
    # Two UPDATE statements should have been executed
    assert db._conn.execute.call_count >= 2


# ---------------------------------------------------------------------------
# 11. set_active — returns False for unknown strategy_id
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_set_active_returns_false_for_unknown_id() -> None:
    db = _make_fake_db()
    db._conn.fetchval = AsyncMock(return_value=None)  # strategy not found
    sm = StrategyManager(db)

    success = await sm.set_active("does_not_exist")

    assert success is False


# ---------------------------------------------------------------------------
# 12. update_config — persists key, invalidates cache
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_update_config_persists_and_invalidates_cache() -> None:
    db = _make_fake_db()
    sm = StrategyManager(db)

    sm._cache = {"min_score_to_buy": 75}
    sm._cache_at = time.monotonic()

    success = await sm.update_config("balanced", "min_score_to_buy", "80")

    assert success is True
    assert sm._cache is None
    db._conn.execute.assert_called_once()
    call_args = db._conn.execute.call_args
    assert "jsonb_set" in call_args[0][0]


# ---------------------------------------------------------------------------
# 13. update_config — returns False when DB returns "UPDATE 0"
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_update_config_returns_false_when_no_rows_updated() -> None:
    db = _make_fake_db()
    db._conn.execute = AsyncMock(return_value="UPDATE 0")
    sm = StrategyManager(db)

    success = await sm.update_config("nonexistent", "min_score_to_buy", 80)

    assert success is False


# ---------------------------------------------------------------------------
# 14. _coerce_value — type coercion for various keys
# ---------------------------------------------------------------------------
def test_coerce_value_int_key_from_string() -> None:
    assert _coerce_value("min_score_to_buy", "80") == 80
    assert isinstance(_coerce_value("min_score_to_buy", "80"), int)


def test_coerce_value_float_key_from_string() -> None:
    result = _coerce_value("max_position_size_sol", "0.07")
    assert result == 0.07
    assert isinstance(result, float)


def test_coerce_value_str_key_preserved() -> None:
    result = _coerce_value("entry_mode", "wait_for_dip")
    assert result == "wait_for_dip"
    assert isinstance(result, str)


def test_coerce_value_negative_float() -> None:
    result = _coerce_value("hard_sl_pct", "-45")
    assert result == -45.0


def test_coerce_value_int_from_float_string() -> None:
    """'80.0' passed for an int key should give 80 (int)."""
    result = _coerce_value("slippage_bps", "1500.0")
    assert result == 1500
    assert isinstance(result, int)


# ---------------------------------------------------------------------------
# 15. strategy_enable_db_override=False — always returns env fallback
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_active_db_override_disabled_returns_env() -> None:
    row = _make_row()
    db = _make_fake_db(fetchrow_return=row)
    sm = StrategyManager(db)

    with patch("src.core.strategy_manager.settings") as mock_settings:
        mock_settings.strategy_enable_db_override = False
        mock_settings.strategy_cache_ttl_seconds = 5
        mock_settings.min_score_to_buy = 99
        mock_settings.max_position_size_sol = 0.01
        mock_settings.max_concurrent_positions = 1
        mock_settings.filter_max_mcap_usd = 50000
        mock_settings.filter_min_liquidity_usd = 5000
        mock_settings.filter_min_gmgn_security_score = 70
        mock_settings.tp1_gain_pct = 80
        mock_settings.tp1_sell_pct = 30
        mock_settings.tp2_gain_pct = 150
        mock_settings.tp2_sell_pct = 30
        mock_settings.tp3_gain_pct = 300
        mock_settings.tp3_sell_pct = 25
        mock_settings.hard_sl_pct = -45
        mock_settings.trailing_stop_pct = 30
        mock_settings.time_based_exit_minutes = 45
        mock_settings.slippage_bps = 1500
        mock_settings.score_weight_smart_money = 35
        mock_settings.score_weight_security = 10

        sm._ttl = 5.0
        config = await sm.get_active()

    # DB must not have been queried
    db._conn.fetchrow.assert_not_called()
    assert config["min_score_to_buy"] == 99
