"""
Backtester Analyzer — evaluate BacktestResult vs decision gate.

Decision gate (revised untuk modal kecil):
- Win rate ≥ 40%
- Profit factor ≥ 1.5
- Max drawdown ≤ 50%
- Total return ≥ 15%
- Min 25 trades dalam 30-day window
"""

from __future__ import annotations

from dataclasses import dataclass

from src.backtester.replay import BacktestResult


@dataclass
class GateThresholds:
    min_win_rate: float = 0.40
    min_profit_factor: float = 1.5
    max_drawdown_pct: float = 50.0
    min_total_return_pct: float = 15.0
    min_trade_count: int = 25


@dataclass
class GateEvaluation:
    passed: bool
    failures: list[str]
    metrics: dict
    thresholds: GateThresholds

    def report(self) -> str:
        lines = []
        lines.append("=" * 60)
        lines.append("BACKTEST DECISION GATE EVALUATION")
        lines.append("=" * 60)
        for key, val in self.metrics.items():
            lines.append(f"  {key}: {val}")
        lines.append("")
        lines.append("Threshold checks:")
        for key, threshold_val in [
            ("win_rate", self.thresholds.min_win_rate),
            ("profit_factor", self.thresholds.min_profit_factor),
            ("max_drawdown_pct", self.thresholds.max_drawdown_pct),
            ("total_return_pct", self.thresholds.min_total_return_pct),
            ("trade_count", self.thresholds.min_trade_count),
        ]:
            failed = any(key in f for f in self.failures)
            status = "❌ FAIL" if failed else "✓ PASS"
            lines.append(f"  {status}  {key} (threshold: {threshold_val})")
        lines.append("")
        if self.passed:
            lines.append("✅ GATE PASSED — strategi VALID. Lanjut ke Phase 3 (live signal).")
        else:
            lines.append("❌ GATE FAILED — strategi belum valid. Refine sebelum lanjut.")
            lines.append("Failure reasons:")
            for f in self.failures:
                lines.append(f"  - {f}")
        lines.append("=" * 60)
        return "\n".join(lines)


def evaluate_decision_gate(
    result: BacktestResult,
    thresholds: GateThresholds | None = None,
) -> GateEvaluation:
    """Evaluate apakah backtest result lolos decision gate."""
    t = thresholds or GateThresholds()
    failures: list[str] = []

    if result.trade_count < t.min_trade_count:
        failures.append(
            f"trade_count too low: {result.trade_count} < {t.min_trade_count} (sample tidak cukup signifikan)"
        )

    if result.win_rate < t.min_win_rate:
        failures.append(
            f"win_rate too low: {result.win_rate:.1%} < {t.min_win_rate:.1%}"
        )

    if result.profit_factor < t.min_profit_factor:
        pf_str = f"{result.profit_factor:.2f}" if result.profit_factor != float("inf") else "inf"
        failures.append(
            f"profit_factor too low: {pf_str} < {t.min_profit_factor}"
        )

    if result.max_drawdown_pct > t.max_drawdown_pct:
        failures.append(
            f"max_drawdown too high: {result.max_drawdown_pct:.1f}% > {t.max_drawdown_pct}%"
        )

    if result.total_return_pct < t.min_total_return_pct:
        failures.append(
            f"total_return too low: {result.total_return_pct:.1f}% < {t.min_total_return_pct}%"
        )

    return GateEvaluation(
        passed=len(failures) == 0,
        failures=failures,
        metrics=result.to_summary_dict(),
        thresholds=t,
    )


def print_trade_breakdown(result: BacktestResult, max_show: int = 20) -> str:
    """Detailed trade-by-trade output untuk inspeksi manual."""
    lines = []
    lines.append("\nTrade Log (showing first 20 + last 10):")
    lines.append(
        f"{'#':<4} {'Symbol':<10} {'Score':<6} {'Entry':<10} {'Exit':<10} "
        f"{'PnL SOL':<10} {'PnL %':<8} {'Reason':<14} {'Min':<6}"
    )
    lines.append("-" * 90)

    sorted_trades = sorted(result.trades, key=lambda t: t.entry_timestamp)
    show_trades = sorted_trades[:max_show] if len(sorted_trades) > max_show else sorted_trades
    if len(sorted_trades) > max_show:
        show_trades = sorted_trades[: max_show // 2] + sorted_trades[-max_show // 2 :]

    for i, t in enumerate(show_trades):
        entry_str = f"${t.entry_price_usd:.6f}"[:10]
        exit_str = f"${t.exit_price_usd:.6f}"[:10]
        symbol = (t.symbol or "?")[:10]
        lines.append(
            f"{i:<4} {symbol:<10} {t.entry_score:<6.1f} {entry_str:<10} {exit_str:<10} "
            f"{t.pnl_sol:>+9.4f}  {t.pnl_pct:>+7.1f}%  {t.exit_reason:<14} {t.holding_minutes:>5.1f}"
        )
    return "\n".join(lines)
