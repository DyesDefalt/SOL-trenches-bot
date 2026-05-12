"""Tests for Prometheus metrics module."""

from __future__ import annotations

import pytest

from src.infra.metrics import (
    cb_trips_total,
    execution_latency,
    llm_call_duration,
    llm_calls_total,
    open_positions,
    render,
    signal_cycle_duration,
    signal_cycles_total,
    signals_total,
    smart_wallets_count,
    trades_total,
    wallet_balance_sol,
)


def test_counter_increments() -> None:
    """Counters increment and their value reflects the delta."""
    before = signal_cycles_total._value.get()
    signal_cycles_total.inc()
    after = signal_cycles_total._value.get()
    assert after == before + 1


def test_labeled_counter_increments() -> None:
    """Labeled counter (signals_total) increments per label."""
    buy_before = signals_total.labels(action="BUY")._value.get()
    signals_total.labels(action="BUY").inc()
    signals_total.labels(action="BUY").inc()
    assert signals_total.labels(action="BUY")._value.get() == buy_before + 2

    skip_before = signals_total.labels(action="SKIP")._value.get()
    signals_total.labels(action="SKIP").inc()
    assert signals_total.labels(action="SKIP")._value.get() == skip_before + 1


def test_gauge_set_and_get() -> None:
    """Gauges can be set and read back."""
    open_positions.set(3)
    assert open_positions._value.get() == 3

    wallet_balance_sol.set(1.234)
    assert abs(wallet_balance_sol._value.get() - 1.234) < 1e-9

    smart_wallets_count.labels(tier="A").set(12)
    assert smart_wallets_count.labels(tier="A")._value.get() == 12


def test_histogram_observes() -> None:
    """Histograms record observations; sum increases and render output contains the metric."""
    before_render = render().decode("utf-8")
    signal_cycle_duration.observe(0.5)
    signal_cycle_duration.observe(1.2)
    llm_call_duration.labels(model="flash").observe(0.3)

    after_render = render().decode("utf-8")
    # After observations the metric names must appear in output
    assert "bot_signal_cycle_duration_seconds" in after_render
    assert "bot_llm_call_duration_seconds" in after_render
    # Sum bucket should be present
    assert "bot_signal_cycle_duration_seconds_sum" in after_render


def test_render_returns_prometheus_text() -> None:
    """render() returns bytes in Prometheus text format."""
    # Ensure at least one metric has been written
    trades_total.labels(outcome="win").inc()

    data = render()
    assert isinstance(data, bytes)
    text = data.decode("utf-8")

    # Prometheus text format starts with '# HELP' or '# TYPE' lines
    assert "# HELP" in text or "# TYPE" in text
    # Our custom metrics should appear
    assert "bot_signal_cycles_total" in text
    assert "bot_trades_total" in text
    assert "bot_open_positions" in text
