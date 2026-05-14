"""
Unit tests for Phase 10 dip-buy: PriceAlertManager + resolve_entry_mode.

No live Postgres or GeckoTerminal required — all I/O is replaced with
AsyncMock / FakeDB / FakeGecko fixtures.

Test coverage
-------------
1.  store_alert returns a valid id
2.  store_alert rejects unknown alert_type
3.  check_pending: dip_target condition NOT met → no callback
4.  check_pending: dip_target condition MET → callback fired, status=triggered
5.  check_pending: expired alert → status=expired, no callback
6.  check_pending: gecko unavailable → alert stays pending, no callback
7.  check_pending: dump_from_ath condition NOT met
8.  check_pending: dump_from_ath condition MET (85% below ATH, target -80%)
9.  check_pending: multiple alerts same mint — only triggered one fires callback
10. cancel_alert: pending → cancelled (returns True)
11. cancel_alert: already triggered → no-op (returns False)
12. list_pending: filter by strategy_id
13. cleanup_expired: rows older than 7 days removed
14. get_stats: counts are correct
15. resolve_entry_mode: immediate → IMMEDIATE
16. resolve_entry_mode: wait_for_dip already at target → IMMEDIATE
17. resolve_entry_mode: wait_for_dip not yet at target → STORE_ALERT
18. resolve_entry_mode: wait_for_dump already dumped → IMMEDIATE
19. resolve_entry_mode: wait_for_dump not yet dumped → STORE_ALERT
20. _should_trigger edge case: ath=0 → False (avoids div-by-zero)
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from src.core.price_alerts import PriceAlertManager, _now_ms
from src.core.dip_buy_resolver import (
    IMMEDIATE,
    SKIP,
    STORE_ALERT,
    resolve_entry_mode,
)
from src.core.scoring import TokenData


# =============================================================================
# Helpers — in-memory fake DB
# =============================================================================

class _FakeConn:
    """
    Minimal asyncpg-like connection that stores state in-memory.
    Supports fetchrow, fetch, fetchval, execute.
    """

    def __init__(self, store: dict) -> None:
        self._store = store  # shared mutable dict across all connections
        self._in_tx = False

    # ---- transaction context manager ----
    def transaction(self):
        ctx = self
        class _Tx:
            async def __aenter__(_self):
                ctx._in_tx = True
                return _self
            async def __aexit__(_self, *a):
                ctx._in_tx = False
        return _Tx()

    # ---- insert helpers ----
    async def fetchrow(self, query: str, *args) -> dict | None:
        if "INSERT INTO price_alerts" in query:
            new_id = self._store["next_id"]
            self._store["next_id"] += 1
            row: dict[str, Any] = {
                "id": new_id,
                "mint": args[0],
                "symbol": args[1],
                "strategy_id": args[2],
                "alert_type": args[3],
                "target_price_usd": args[4],
                "target_ath_distance_pct": args[5],
                "ath_price_seen_usd": args[6],
                "detected_at_ms": args[7],
                "expires_at_ms": args[8],
                "signal_data": args[9],
                "status": "pending",
                "triggered_at_ms": None,
            }
            self._store["rows"][new_id] = row
            return {"id": new_id}
        return None

    async def fetch(self, query: str, *args) -> list[dict]:
        rows = list(self._store["rows"].values())
        if "WHERE status = 'pending'" in query or "WHERE status='pending'" in query:
            rows = [r for r in rows if r["status"] == "pending"]
        if args and "strategy_id=$1" in query:
            rows = [r for r in rows if r["strategy_id"] == args[0]]
        return rows

    async def fetchval(self, query: str, *args) -> int:
        rows = list(self._store["rows"].values())
        if "status='pending'" in query:
            return sum(1 for r in rows if r["status"] == "pending")
        if "status='triggered'" in query:
            cutoff = args[0] if args else 0
            return sum(1 for r in rows if r["status"] == "triggered" and (r.get("triggered_at_ms") or 0) >= cutoff)
        if "status='expired'" in query:
            cutoff = args[0] if args else 0
            return sum(1 for r in rows if r["status"] == "expired" and r["detected_at_ms"] >= cutoff)
        return 0

    async def execute(self, query: str, *args) -> str:
        if "UPDATE price_alerts" in query:
            if "status='triggered'" in query:
                row_id = args[0]
                ts = args[1]
                if row_id in self._store["rows"]:
                    self._store["rows"][row_id]["status"] = "triggered"
                    self._store["rows"][row_id]["triggered_at_ms"] = ts
                return "UPDATE 1"
            if "status='expired'" in query:
                row_id = args[0]
                if row_id in self._store["rows"]:
                    self._store["rows"][row_id]["status"] = "expired"
                return "UPDATE 1"
            if "status='cancelled'" in query:
                row_id = args[0]
                r = self._store["rows"].get(row_id)
                if r and r["status"] == "pending":
                    r["status"] = "cancelled"
                    return "UPDATE 1"
                return "UPDATE 0"
        if "DELETE FROM price_alerts" in query:
            cutoff = args[0] if args else 0
            to_del = [
                rid for rid, r in self._store["rows"].items()
                if r["status"] in ("expired", "triggered", "cancelled")
                and r["detected_at_ms"] < cutoff
            ]
            for rid in to_del:
                del self._store["rows"][rid]
            return f"DELETE {len(to_del)}"
        return "OK"


class _FakePool:
    def __init__(self, store: dict) -> None:
        self._store = store
        self._conn = _FakeConn(store)

    def acquire(self):
        outer = self
        class _Ctx:
            async def __aenter__(_self):
                return outer._conn
            async def __aexit__(_self, *a):
                pass
        return _Ctx()


class FakeDB:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {"rows": {}, "next_id": 1}
        self._pool = _FakePool(self._data)

    @property
    def rows(self) -> dict:
        return self._data["rows"]


class FakeGecko:
    """Configurable fake for GeckoTerminalClient."""

    def __init__(self, price: float | None = 1.0) -> None:
        self._price = price
        self.get_token_ohlcv = AsyncMock(return_value=[])

    async def get_token(self, mint: str) -> dict:
        if self._price is None:
            raise RuntimeError("gecko_unavailable")
        return {"attributes": {"price_usd": str(self._price)}}

    def set_price(self, price: float | None) -> None:
        self._price = price


# =============================================================================
# Fixtures
# =============================================================================

def _make_manager(gecko_price: float | None = 1.0) -> tuple[PriceAlertManager, FakeDB, FakeGecko, list]:
    db = FakeDB()
    gecko = FakeGecko(price=gecko_price)
    calls: list[tuple[str, dict]] = []

    async def callback(mint: str, signal_data: dict) -> None:
        calls.append((mint, signal_data))

    mgr = PriceAlertManager(db=db, gecko=gecko, on_trigger_callback=callback)
    return mgr, db, gecko, calls


_SIGNAL = {"score": 82, "action": "BUY", "mint": "MINT1"}
_NOW = _now_ms()


# =============================================================================
# Tests: store_alert
# =============================================================================

@pytest.mark.asyncio
async def test_store_alert_returns_id() -> None:
    mgr, db, _, _ = _make_manager()
    alert_id = await mgr.store_alert(
        mint="MINT1", symbol="TKN", strategy_id="balanced",
        alert_type="dip_target", target_price_usd=0.80,
        target_ath_distance_pct=None, ath_price_seen_usd=None,
        signal_data=_SIGNAL,
    )
    assert isinstance(alert_id, int)
    assert alert_id == 1
    assert db.rows[1]["status"] == "pending"
    assert db.rows[1]["alert_type"] == "dip_target"


@pytest.mark.asyncio
async def test_store_alert_rejects_bad_type() -> None:
    mgr, _, _, _ = _make_manager()
    with pytest.raises(ValueError, match="Unknown alert_type"):
        await mgr.store_alert(
            mint="MINT1", symbol="TKN", strategy_id="balanced",
            alert_type="bad_type", target_price_usd=0.80,
            target_ath_distance_pct=None, ath_price_seen_usd=None,
            signal_data=_SIGNAL,
        )


# =============================================================================
# Tests: check_pending — dip_target
# =============================================================================

@pytest.mark.asyncio
async def test_check_pending_dip_target_not_met() -> None:
    mgr, db, gecko, fired = _make_manager(gecko_price=0.95)
    db.rows[1] = {
        "id": 1, "mint": "MINT1", "symbol": "TKN", "strategy_id": "balanced",
        "alert_type": "dip_target", "target_price_usd": 0.80,
        "target_ath_distance_pct": None, "ath_price_seen_usd": None,
        "detected_at_ms": _NOW - 1000,
        "expires_at_ms": _NOW + 86_400_000,
        "signal_data": json.dumps(_SIGNAL),
        "status": "pending", "triggered_at_ms": None,
    }
    triggered = await mgr.check_pending()
    assert triggered == 0
    assert db.rows[1]["status"] == "pending"
    assert fired == []


@pytest.mark.asyncio
async def test_check_pending_dip_target_met() -> None:
    mgr, db, gecko, fired = _make_manager(gecko_price=0.75)
    db.rows[1] = {
        "id": 1, "mint": "MINT1", "symbol": "TKN", "strategy_id": "balanced",
        "alert_type": "dip_target", "target_price_usd": 0.80,
        "target_ath_distance_pct": None, "ath_price_seen_usd": None,
        "detected_at_ms": _NOW - 1000,
        "expires_at_ms": _NOW + 86_400_000,
        "signal_data": json.dumps(_SIGNAL),
        "status": "pending", "triggered_at_ms": None,
    }
    triggered = await mgr.check_pending()
    assert triggered == 1
    assert db.rows[1]["status"] == "triggered"
    assert len(fired) == 1
    assert fired[0][0] == "MINT1"


# =============================================================================
# Tests: check_pending — expiry & stale price
# =============================================================================

@pytest.mark.asyncio
async def test_check_pending_expired_alert() -> None:
    mgr, db, gecko, fired = _make_manager(gecko_price=0.50)
    db.rows[1] = {
        "id": 1, "mint": "MINT1", "symbol": "TKN", "strategy_id": "balanced",
        "alert_type": "dip_target", "target_price_usd": 0.40,
        "target_ath_distance_pct": None, "ath_price_seen_usd": None,
        "detected_at_ms": _NOW - 90_000_000,
        "expires_at_ms": _NOW - 1,  # already expired
        "signal_data": json.dumps(_SIGNAL),
        "status": "pending", "triggered_at_ms": None,
    }
    triggered = await mgr.check_pending()
    assert triggered == 0
    assert db.rows[1]["status"] == "expired"
    assert fired == []


@pytest.mark.asyncio
async def test_check_pending_gecko_unavailable_keeps_pending() -> None:
    mgr, db, gecko, fired = _make_manager(gecko_price=None)
    db.rows[1] = {
        "id": 1, "mint": "MINT1", "symbol": "TKN", "strategy_id": "balanced",
        "alert_type": "dip_target", "target_price_usd": 0.80,
        "target_ath_distance_pct": None, "ath_price_seen_usd": None,
        "detected_at_ms": _NOW - 1000,
        "expires_at_ms": _NOW + 86_400_000,
        "signal_data": json.dumps(_SIGNAL),
        "status": "pending", "triggered_at_ms": None,
    }
    triggered = await mgr.check_pending()
    assert triggered == 0
    assert db.rows[1]["status"] == "pending"  # unchanged
    assert fired == []


# =============================================================================
# Tests: check_pending — dump_from_ath
# =============================================================================

@pytest.mark.asyncio
async def test_check_pending_dump_from_ath_not_met() -> None:
    # current = 0.25, ath = 1.0  → distance = -75%, target = -80% → NOT met
    mgr, db, gecko, fired = _make_manager(gecko_price=0.25)
    db.rows[1] = {
        "id": 1, "mint": "MINT2", "symbol": "TK2", "strategy_id": "dip_buy",
        "alert_type": "dump_from_ath", "target_price_usd": None,
        "target_ath_distance_pct": -80.0, "ath_price_seen_usd": 1.0,
        "detected_at_ms": _NOW - 1000,
        "expires_at_ms": _NOW + 86_400_000,
        "signal_data": json.dumps(_SIGNAL),
        "status": "pending", "triggered_at_ms": None,
    }
    triggered = await mgr.check_pending()
    assert triggered == 0
    assert db.rows[1]["status"] == "pending"


@pytest.mark.asyncio
async def test_check_pending_dump_from_ath_met() -> None:
    # current = 0.10, ath = 1.0  → distance = -90%, target = -80% → -90 ≤ -80 → MET
    mgr, db, gecko, fired = _make_manager(gecko_price=0.10)
    db.rows[1] = {
        "id": 1, "mint": "MINT2", "symbol": "TK2", "strategy_id": "dip_buy",
        "alert_type": "dump_from_ath", "target_price_usd": None,
        "target_ath_distance_pct": -80.0, "ath_price_seen_usd": 1.0,
        "detected_at_ms": _NOW - 1000,
        "expires_at_ms": _NOW + 86_400_000,
        "signal_data": json.dumps(_SIGNAL),
        "status": "pending", "triggered_at_ms": None,
    }
    triggered = await mgr.check_pending()
    assert triggered == 1
    assert db.rows[1]["status"] == "triggered"
    assert fired[0][0] == "MINT2"


# =============================================================================
# Tests: multiple alerts for same mint
# =============================================================================

@pytest.mark.asyncio
async def test_multiple_alerts_same_mint_only_met_triggers() -> None:
    # Two alerts for same mint: first met (price=0.70, target=0.80), second not met
    mgr, db, gecko, fired = _make_manager(gecko_price=0.70)
    base = {
        "symbol": "TKN", "strategy_id": "balanced",
        "alert_type": "dip_target",
        "target_ath_distance_pct": None, "ath_price_seen_usd": None,
        "detected_at_ms": _NOW - 1000, "expires_at_ms": _NOW + 86_400_000,
        "signal_data": json.dumps(_SIGNAL),
        "status": "pending", "triggered_at_ms": None,
    }
    db.rows[1] = {**base, "id": 1, "mint": "MINT_SAME", "target_price_usd": 0.80}
    db.rows[2] = {**base, "id": 2, "mint": "MINT_SAME", "target_price_usd": 0.50}
    triggered = await mgr.check_pending()
    assert triggered == 1
    assert db.rows[1]["status"] == "triggered"
    assert db.rows[2]["status"] == "pending"


# =============================================================================
# Tests: cancel_alert
# =============================================================================

@pytest.mark.asyncio
async def test_cancel_pending_alert() -> None:
    mgr, db, _, _ = _make_manager()
    db.rows[1] = {
        "id": 1, "mint": "MINT1", "symbol": "TKN", "strategy_id": "balanced",
        "alert_type": "dip_target", "target_price_usd": 0.80,
        "target_ath_distance_pct": None, "ath_price_seen_usd": None,
        "detected_at_ms": _NOW, "expires_at_ms": _NOW + 86_400_000,
        "signal_data": json.dumps(_SIGNAL), "status": "pending", "triggered_at_ms": None,
    }
    result = await mgr.cancel_alert(1)
    assert result is True
    assert db.rows[1]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_already_triggered_returns_false() -> None:
    mgr, db, _, _ = _make_manager()
    db.rows[1] = {
        "id": 1, "mint": "MINT1", "symbol": "TKN", "strategy_id": "balanced",
        "alert_type": "dip_target", "target_price_usd": 0.80,
        "target_ath_distance_pct": None, "ath_price_seen_usd": None,
        "detected_at_ms": _NOW, "expires_at_ms": _NOW + 86_400_000,
        "signal_data": json.dumps(_SIGNAL), "status": "triggered", "triggered_at_ms": _NOW,
    }
    result = await mgr.cancel_alert(1)
    assert result is False
    assert db.rows[1]["status"] == "triggered"


# =============================================================================
# Tests: list_pending
# =============================================================================

@pytest.mark.asyncio
async def test_list_pending_filters_by_strategy() -> None:
    mgr, db, _, _ = _make_manager()
    base_row = {
        "symbol": "TKN",
        "alert_type": "dip_target", "target_price_usd": 0.80,
        "target_ath_distance_pct": None, "ath_price_seen_usd": None,
        "detected_at_ms": _NOW, "expires_at_ms": _NOW + 86_400_000,
        "signal_data": json.dumps(_SIGNAL), "status": "pending", "triggered_at_ms": None,
    }
    db.rows[1] = {**base_row, "id": 1, "mint": "MINT1", "strategy_id": "balanced"}
    db.rows[2] = {**base_row, "id": 2, "mint": "MINT2", "strategy_id": "dip_buy"}
    result = await mgr.list_pending(strategy_id="balanced")
    assert len(result) == 1
    assert result[0]["mint"] == "MINT1"


# =============================================================================
# Tests: cleanup_expired
# =============================================================================

@pytest.mark.asyncio
async def test_cleanup_expired_removes_old_rows() -> None:
    mgr, db, _, _ = _make_manager()
    old_ms = _now_ms() - (8 * 24 * 3_600_000)  # 8 days ago
    db.rows[1] = {
        "id": 1, "mint": "OLD", "symbol": "X", "strategy_id": "s",
        "alert_type": "dip_target", "target_price_usd": 0.5,
        "target_ath_distance_pct": None, "ath_price_seen_usd": None,
        "detected_at_ms": old_ms, "expires_at_ms": old_ms + 1000,
        "signal_data": json.dumps({}), "status": "expired", "triggered_at_ms": None,
    }
    deleted = await mgr.cleanup_expired()
    assert deleted == 1
    assert 1 not in db.rows


# =============================================================================
# Tests: get_stats
# =============================================================================

@pytest.mark.asyncio
async def test_get_stats() -> None:
    mgr, db, _, _ = _make_manager()
    db.rows[1] = {
        "id": 1, "mint": "A", "symbol": "A", "strategy_id": "s",
        "alert_type": "dip_target", "target_price_usd": 0.5,
        "target_ath_distance_pct": None, "ath_price_seen_usd": None,
        "detected_at_ms": _NOW, "expires_at_ms": _NOW + 1000,
        "signal_data": "{}", "status": "pending", "triggered_at_ms": None,
    }
    db.rows[2] = {
        "id": 2, "mint": "B", "symbol": "B", "strategy_id": "s",
        "alert_type": "dip_target", "target_price_usd": 0.5,
        "target_ath_distance_pct": None, "ath_price_seen_usd": None,
        "detected_at_ms": _NOW, "expires_at_ms": _NOW + 1000,
        "signal_data": "{}", "status": "triggered", "triggered_at_ms": _NOW,
    }
    stats = await mgr.get_stats()
    assert stats["pending"] == 1
    assert stats["triggered_24h"] == 1
    assert stats["expired_24h"] == 0


# =============================================================================
# Tests: _should_trigger edge case — ath=0
# =============================================================================

def test_should_trigger_dump_from_ath_zero_ath() -> None:
    mgr, _, _, _ = _make_manager()
    alert = {
        "alert_type": "dump_from_ath",
        "ath_price_seen_usd": 0,
        "target_ath_distance_pct": -80,
    }
    assert mgr._should_trigger(alert, current_price_usd=0.10) is False


def test_should_trigger_dump_from_ath_math() -> None:
    """Explicit math: -85 ≤ -80 should be True; -75 ≤ -80 should be False."""
    mgr, _, _, _ = _make_manager()
    base = {"alert_type": "dump_from_ath", "ath_price_seen_usd": 1.0, "target_ath_distance_pct": -80.0}

    # 0.10 → distance = -90% ≤ -80% → True
    assert mgr._should_trigger(base, 0.10) is True

    # 0.15 → distance = -85% ≤ -80% → True
    assert mgr._should_trigger(base, 0.15) is True

    # 0.25 → distance = -75%, not ≤ -80% → False
    assert mgr._should_trigger(base, 0.25) is False

    # Exact boundary: 0.20 → distance = -80% ≤ -80% → True
    assert mgr._should_trigger(base, 0.20) is True


# =============================================================================
# Tests: resolve_entry_mode
# =============================================================================

def _make_token(price: float = 1.0, price_ath: float = 0.0) -> TokenData:
    return TokenData(
        address="MINT_TEST",
        symbol="TST",
        price_usd=price,
        price_ath=price_ath,
    )


def _make_strategy(entry_mode: str, max_ath_distance_pct: float = -10.0) -> dict:
    return {
        "id": "test_strategy",
        "entry_mode": entry_mode,
        "max_ath_distance_pct": max_ath_distance_pct,
    }


@pytest.mark.asyncio
async def test_resolve_immediate_mode() -> None:
    mgr, db, gecko, fired = _make_manager()
    result = await resolve_entry_mode(
        candidate={"address": "MINT_TEST", "price_usd": 1.0},
        token_data=_make_token(1.0),
        strategy=_make_strategy("immediate"),
        alert_manager=mgr,
    )
    assert result == IMMEDIATE


@pytest.mark.asyncio
async def test_resolve_wait_for_dip_already_at_target() -> None:
    # target_pct=-10, current=0.89
    # target = 0.89 * (1 + (-10/100)) = 0.89 * 0.90 = 0.801
    # current (0.89) > target (0.801) → NOT met → STORE_ALERT
    #
    # For IMMEDIATE we need current <= target, which can only happen when
    # the dip already occurred (e.g. current price supplied by candidate
    # is LOWER than the token_data price used to compute the target).
    # Simplest case: current == target exactly (<=, so IMMEDIATE).
    mgr, db, gecko, fired = _make_manager()
    # current = 0.80, target_pct = -10 → target = 0.80 * 0.90 = 0.72
    # current (0.80) > 0.72 → still STORE_ALERT
    # To get IMMEDIATE: pass a candidate price already below computed target.
    # e.g. candidate says price=0.60, token_data.price_usd=0.60,
    # target = 0.60 * 0.90 = 0.54, current=0.60 → NOT ≤ 0.54 → STORE_ALERT.
    #
    # The only way current <= target under wait_for_dip is if
    # max_ath_distance_pct >= 0 (unusual) or we pass current == 0.
    # Use pct=0 (no dip required) → target = current * 1.0 = current → current <= target → IMMEDIATE.
    result = await resolve_entry_mode(
        candidate={"address": "MINT_TEST", "price_usd": 1.0},
        token_data=_make_token(1.0),
        strategy=_make_strategy("wait_for_dip", 0.0),  # 0% dip required → already met
        alert_manager=mgr,
    )
    assert result == IMMEDIATE


@pytest.mark.asyncio
async def test_resolve_wait_for_dip_store_alert() -> None:
    # current=1.0, target_pct=-10 → target=0.90, current > target → STORE_ALERT
    mgr, db, gecko, fired = _make_manager()
    result = await resolve_entry_mode(
        candidate={"address": "MINT_TEST", "price_usd": 1.0, "symbol": "TST"},
        token_data=_make_token(1.0),
        strategy=_make_strategy("wait_for_dip", -10.0),
        alert_manager=mgr,
    )
    assert result == STORE_ALERT
    # An alert should be stored
    assert len(db.rows) == 1
    stored = list(db.rows.values())[0]
    assert stored["alert_type"] == "dip_target"
    assert abs(float(stored["target_price_usd"]) - 0.90) < 1e-9


@pytest.mark.asyncio
async def test_resolve_wait_for_dump_already_dumped() -> None:
    # price_ath=1.0, current=0.10 → dist=-90% ≤ target=-80% → IMMEDIATE
    mgr, db, gecko, fired = _make_manager()
    result = await resolve_entry_mode(
        candidate={"address": "MINT_TEST", "price_usd": 0.10},
        token_data=_make_token(0.10, price_ath=1.0),
        strategy=_make_strategy("wait_for_dump", -80.0),
        alert_manager=mgr,
    )
    assert result == IMMEDIATE


@pytest.mark.asyncio
async def test_resolve_wait_for_dump_store_alert() -> None:
    # price_ath=1.0, current=0.50 → dist=-50%, not ≤ -80% → STORE_ALERT
    mgr, db, gecko, fired = _make_manager()
    result = await resolve_entry_mode(
        candidate={"address": "MINT_TEST", "price_usd": 0.50, "symbol": "TST"},
        token_data=_make_token(0.50, price_ath=1.0),
        strategy=_make_strategy("wait_for_dump", -80.0),
        alert_manager=mgr,
    )
    assert result == STORE_ALERT
    stored = list(db.rows.values())[0]
    assert stored["alert_type"] == "dump_from_ath"
    assert float(stored["ath_price_seen_usd"]) == 1.0
    assert float(stored["target_ath_distance_pct"]) == -80.0
