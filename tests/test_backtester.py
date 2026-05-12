"""Unit tests untuk backtester replay engine."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.backtester.analyze import GateThresholds, evaluate_decision_gate
from src.backtester.replay import BacktestResult, ReplayEngine, SimulatedTrade


def _make_trade(pnl_sol: float, won: bool) -> SimulatedTrade:
    return SimulatedTrade(
        token_address="x",
        symbol="X",
        entry_timestamp=datetime.now(timezone.utc),
        entry_price_usd=0.001,
        entry_amount_sol=0.025,
        entry_score=80,
        exit_timestamp=datetime.now(timezone.utc),
        exit_price_usd=0.002,
        exit_amount_sol=0.05,
        exit_reason="TP1",
        pnl_sol=pnl_sol,
        pnl_pct=pnl_sol / 0.025 * 100,
        won=won,
        holding_minutes=15,
    )


def test_empty_result_metrics() -> None:
    r = BacktestResult(initial_capital_sol=0.36)
    assert r.trade_count == 0
    assert r.win_rate == 0
    assert r.total_pnl_sol == 0
    assert r.profit_factor == 0


def test_single_winning_trade() -> None:
    r = BacktestResult(initial_capital_sol=0.36)
    r.trades.append(_make_trade(pnl_sol=0.01, won=True))

    assert r.trade_count == 1
    assert r.win_rate == 1.0
    assert r.total_pnl_sol == 0.01
    assert r.gross_profit == 0.01
    assert r.gross_loss == 0.0
    assert r.profit_factor == float("inf")


def test_mixed_trades_metrics() -> None:
    r = BacktestResult(initial_capital_sol=0.36)
    r.trades.extend([
        _make_trade(pnl_sol=0.02, won=True),
        _make_trade(pnl_sol=0.015, won=True),
        _make_trade(pnl_sol=-0.005, won=False),
        _make_trade(pnl_sol=-0.005, won=False),
    ])

    assert r.trade_count == 4
    assert r.win_rate == 0.5
    assert abs(r.total_pnl_sol - 0.025) < 1e-9
    assert abs(r.gross_profit - 0.035) < 1e-9
    assert abs(r.gross_loss - 0.010) < 1e-9
    assert abs(r.profit_factor - 3.5) < 1e-9


def test_max_drawdown_calc() -> None:
    """Drawdown calculation dari peak equity."""
    r = BacktestResult(initial_capital_sol=1.0)
    # Equity walk: 1.0 → 1.5 (peak) → 1.0 → 1.2 → 0.7 (max DD from peak 1.5)
    trades = [
        _make_trade(pnl_sol=0.5, won=True),    # +0.5 → 1.5
        _make_trade(pnl_sol=-0.5, won=False),  # -0.5 → 1.0
        _make_trade(pnl_sol=0.2, won=True),    # +0.2 → 1.2
        _make_trade(pnl_sol=-0.5, won=False),  # -0.5 → 0.7
    ]
    # Adjust timestamps supaya order benar
    for i, t in enumerate(trades):
        from datetime import timedelta

        t.exit_timestamp = datetime.now(timezone.utc) + timedelta(minutes=i)
        r.trades.append(t)

    # Max DD = (0.7 - 1.5) / 1.5 = -53.3%
    assert r.max_drawdown_pct == pytest.approx(53.33, abs=0.5)


def test_decision_gate_pass() -> None:
    """Gate harus pass kalau metrics melebihi threshold."""
    r = BacktestResult(initial_capital_sol=0.36)
    # 30 trades: 15 wins ($0.02 each), 15 losses ($0.005 each)
    # Win rate 50%, profit factor (15*0.02)/(15*0.005) = 4.0, return: (0.225)/0.36 = 62.5%
    for i in range(15):
        r.trades.append(_make_trade(pnl_sol=0.02, won=True))
    for i in range(15):
        r.trades.append(_make_trade(pnl_sol=-0.005, won=False))

    eval_result = evaluate_decision_gate(r)
    assert eval_result.passed is True
    assert eval_result.failures == []


def test_decision_gate_fail_low_winrate() -> None:
    r = BacktestResult(initial_capital_sol=0.36)
    # 30 trades dengan win rate 30% (di bawah threshold 40%)
    for i in range(9):
        r.trades.append(_make_trade(pnl_sol=0.05, won=True))
    for i in range(21):
        r.trades.append(_make_trade(pnl_sol=-0.005, won=False))

    eval_result = evaluate_decision_gate(r)
    assert eval_result.passed is False
    assert any("win_rate" in f for f in eval_result.failures)


def test_decision_gate_fail_low_trade_count() -> None:
    """Trade count < 25 → fail."""
    r = BacktestResult(initial_capital_sol=0.36)
    for i in range(10):
        r.trades.append(_make_trade(pnl_sol=0.05, won=True))

    eval_result = evaluate_decision_gate(r)
    assert eval_result.passed is False
    assert any("trade_count" in f for f in eval_result.failures)


def test_decision_gate_custom_thresholds() -> None:
    """Custom thresholds harus dipakai."""
    r = BacktestResult(initial_capital_sol=0.36)
    for i in range(10):
        r.trades.append(_make_trade(pnl_sol=0.05, won=True))

    # Loosen threshold: trade_count min 5, total_return min 5%
    custom = GateThresholds(
        min_trade_count=5,
        min_win_rate=0.50,
        min_profit_factor=1.0,
        max_drawdown_pct=80,
        min_total_return_pct=5,
    )
    eval_result = evaluate_decision_gate(r, thresholds=custom)
    assert eval_result.passed is True


def test_replay_engine_empty_dataset() -> None:
    engine = ReplayEngine()
    result = engine.run([])
    assert result.trade_count == 0


def test_replay_engine_skips_short_history() -> None:
    """Token dengan candles <30 di-skip (tidak cukup history)."""
    engine = ReplayEngine()
    short_data = [{
        "address": "x",
        "symbol": "X",
        "metadata": {},
        "ohlcv": [[i * 60, 0.001, 0.001, 0.001, 0.001, 1000] for i in range(20)],  # only 20 candles
    }]
    result = engine.run(short_data)
    assert result.trade_count == 0


def test_replay_engine_processes_valid_dataset() -> None:
    """Dataset valid harus produksi hasil (mungkin 0 atau lebih trade)."""
    engine = ReplayEngine(rng_seed=42)
    # Generate 100 candles dengan price action realistis
    base_price = 0.001
    ohlcv = []
    for i in range(100):
        price = base_price * (1 + (i % 10) * 0.05)  # oscillating
        ohlcv.append([
            i * 300,            # ts (5-minute candles)
            price * 0.99,       # open
            price * 1.05,       # high
            price * 0.95,       # low
            price,              # close
            10000 + i * 100,    # volume increasing
        ])

    dataset = [{
        "address": "x",
        "symbol": "X",
        "metadata": {"decimals": 9, "total_supply": 1_000_000_000},
        "ohlcv": ohlcv,
    }]
    result = engine.run(dataset)
    # Just verify run tidak crash dan return valid result
    assert isinstance(result, BacktestResult)
    assert result.initial_capital_sol == 0.36
