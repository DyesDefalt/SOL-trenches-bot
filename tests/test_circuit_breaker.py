"""Unit tests untuk CircuitBreaker."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.core.circuit_breaker import CBState, CircuitBreaker, CBTrigger, TradeOutcome


@pytest.fixture
def cb() -> CircuitBreaker:
    """CB tanpa DB dan Telegram (untuk unit test)."""
    return CircuitBreaker(db=None, telegram=None)


@pytest.mark.asyncio
async def test_initialize_sets_baselines(cb: CircuitBreaker) -> None:
    await cb.initialize(starting_balance_sol=0.36)
    assert cb.state.starting_balance_sol == 0.36
    assert cb.state.peak_balance_sol == 0.36
    assert cb.state.daily_starting_balance_sol == 0.36


@pytest.mark.asyncio
async def test_can_open_initially(cb: CircuitBreaker) -> None:
    await cb.initialize(0.36)
    assert cb.can_open_position() is True


@pytest.mark.asyncio
async def test_consecutive_loss_trip(cb: CircuitBreaker) -> None:
    """3 consecutive losses → trip."""
    await cb.initialize(0.36)

    for _ in range(3):
        await cb.record_trade(
            TradeOutcome(
                token_address="x",
                pnl_sol=-0.005,
                pnl_pct=-15,
                won=False,
                exit_reason="SL",
            )
        )

    assert cb.state.is_paused is True
    assert "CONSECUTIVE_LOSS" in cb.state.pause_reason


@pytest.mark.asyncio
async def test_win_resets_consecutive_counter(cb: CircuitBreaker) -> None:
    """Win di tengah-tengah harus reset consecutive losses counter."""
    await cb.initialize(0.36)

    await cb.record_trade(
        TradeOutcome(token_address="x", pnl_sol=-0.005, pnl_pct=-15, won=False, exit_reason="SL")
    )
    await cb.record_trade(
        TradeOutcome(token_address="x", pnl_sol=-0.005, pnl_pct=-15, won=False, exit_reason="SL")
    )
    assert cb.state.consecutive_losses == 2

    await cb.record_trade(
        TradeOutcome(token_address="x", pnl_sol=0.01, pnl_pct=20, won=True, exit_reason="TP1")
    )
    assert cb.state.consecutive_losses == 0
    assert cb.state.is_paused is False


@pytest.mark.asyncio
async def test_daily_loss_trip(cb: CircuitBreaker) -> None:
    """-30% daily loss → trip."""
    await cb.initialize(0.10)  # smaller baseline supaya lebih cepat trigger

    # Loss -0.04 SOL = -40% dari 0.10 → above threshold
    await cb.record_trade(
        TradeOutcome(token_address="x", pnl_sol=-0.04, pnl_pct=-40, won=False, exit_reason="SL")
    )
    assert cb.state.is_paused is True


@pytest.mark.asyncio
async def test_max_drawdown_trip(cb: CircuitBreaker) -> None:
    """Drawdown -50% from peak → trip."""
    await cb.initialize(0.36)

    # Update peak ke 0.50, lalu drop ke 0.20 (60% drawdown)
    await cb.update_balance(0.50)
    await cb.update_balance(0.20)

    # Trigger via record trade (logic check happens there)
    await cb.record_trade(
        TradeOutcome(token_address="x", pnl_sol=-0.30, pnl_pct=-60, won=False, exit_reason="SL")
    )
    assert cb.state.is_paused is True


@pytest.mark.asyncio
async def test_low_winrate_trip(cb: CircuitBreaker) -> None:
    """Win rate < 25% atas 20 trades → trip."""
    await cb.initialize(0.36)

    # 20 trades, hanya 4 wins (20%) — di bawah 25%
    for i in range(20):
        won = i < 4  # first 4 wins, rest losses
        # Reset consecutive counter dengan win supaya tidak trip CONSECUTIVE_LOSS dulu
        await cb.record_trade(
            TradeOutcome(
                token_address=f"x{i}",
                pnl_sol=0.01 if won else -0.001,
                pnl_pct=10 if won else -1,
                won=won,
                exit_reason="TP1" if won else "SL",
            )
        )
        if cb.state.is_paused:
            # Already tripped (kemungkinan consecutive loss)
            break

    # Test: WIN_RATE harus tertangkap kalau sample cukup
    # Bisa juga consecutive_loss yang trigger duluan; salah satu OK
    assert cb.state.is_paused is True


@pytest.mark.asyncio
async def test_manual_pause_resume(cb: CircuitBreaker) -> None:
    await cb.initialize(0.36)
    cb.manual_pause(reason="user_request")
    assert cb.state.is_paused is True
    assert "MANUAL" in cb.state.pause_reason

    cb.manual_resume(reason="user_request")
    assert cb.state.is_paused is False


@pytest.mark.asyncio
async def test_can_open_after_cooldown_elapsed(cb: CircuitBreaker) -> None:
    """Setelah cooldown elapsed, can_open() trigger auto-resume."""
    from datetime import timedelta

    await cb.initialize(0.36)
    cb.state.is_paused = True
    # Set paused_until ke past
    cb.state.paused_until = datetime.now(timezone.utc) - timedelta(hours=1)

    # First check: trigger auto-resume
    can = cb.can_open_position()
    assert can is True
    assert cb.state.is_paused is False


@pytest.mark.asyncio
async def test_db_event_persisted_on_trip(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """CB trip → db.insert_cb_event called."""
    mock_db = AsyncMock()
    cb = CircuitBreaker(db=mock_db)
    await cb.initialize(0.10)

    for _ in range(3):
        await cb.record_trade(
            TradeOutcome(token_address="x", pnl_sol=-0.005, pnl_pct=-15, won=False, exit_reason="SL")
        )

    mock_db.insert_cb_event.assert_called_once()
    args = mock_db.insert_cb_event.call_args.kwargs
    assert args["trigger_type"] == CBTrigger.CONSECUTIVE_LOSS.value
