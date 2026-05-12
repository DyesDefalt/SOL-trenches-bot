"""
Prometheus metrics for bot observability.

Counters:
- bot_signal_cycles_total
- bot_signals_total{action="BUY|ALERT|SKIP|REJECT"}
- bot_trades_total{outcome="win|loss"}
- bot_circuit_breaker_trips_total{trigger="..."}
- bot_llm_calls_total{model="..."}

Gauges:
- bot_open_positions
- bot_wallet_balance_sol
- bot_smart_wallets_count{tier="A|B|C|..."}
- bot_llm_daily_cost_usd

Histograms:
- bot_signal_cycle_duration_seconds
- bot_execution_latency_seconds
- bot_llm_call_duration_seconds{model="..."}

Usage:
    from src.infra.metrics import signal_cycles_total, render
    signal_cycles_total.inc()
    data: bytes = render()
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, generate_latest

# ------------------------------------------------------------------
# Counters
# ------------------------------------------------------------------

signal_cycles_total = Counter(
    "bot_signal_cycles_total",
    "Total signal cycles completed",
)

signals_total = Counter(
    "bot_signals_total",
    "Total signals processed, labelled by action",
    ["action"],
)

trades_total = Counter(
    "bot_trades_total",
    "Total closed trades by outcome (win|loss)",
    ["outcome"],
)

cb_trips_total = Counter(
    "bot_circuit_breaker_trips_total",
    "Circuit breaker trip events",
    ["trigger"],
)

llm_calls_total = Counter(
    "bot_llm_calls_total",
    "Total LLM API calls by model",
    ["model"],
)

# ------------------------------------------------------------------
# Gauges
# ------------------------------------------------------------------

open_positions = Gauge(
    "bot_open_positions",
    "Current number of open positions",
)

wallet_balance_sol = Gauge(
    "bot_wallet_balance_sol",
    "Bot wallet SOL balance",
)

smart_wallets_count = Gauge(
    "bot_smart_wallets_count",
    "Number of tracked smart wallets by tier",
    ["tier"],
)

llm_daily_cost = Gauge(
    "bot_llm_daily_cost_usd",
    "Estimated LLM daily spend in USD",
)

# ------------------------------------------------------------------
# Histograms
# ------------------------------------------------------------------

signal_cycle_duration = Histogram(
    "bot_signal_cycle_duration_seconds",
    "Duration of a full signal cycle in seconds",
)

execution_latency = Histogram(
    "bot_execution_latency_seconds",
    "Trade execution latency from initiation to submission in seconds",
)

llm_call_duration = Histogram(
    "bot_llm_call_duration_seconds",
    "LLM API call round-trip duration in seconds",
    ["model"],
)


# ------------------------------------------------------------------
# Renderer
# ------------------------------------------------------------------

def render() -> bytes:
    """Return current metrics in Prometheus text exposition format."""
    return generate_latest()
