"""
Run backtest end-to-end. Fetch data → replay → evaluate decision gate.

Usage:
    python scripts/run_backtest.py [--sample 50] [--cached]

Flags:
    --sample N    : jumlah token untuk di-test (default 50, free tier rate limit consideration)
    --cached      : skip fetch baru, pakai data di data/backtest_cache/

Output:
    - Console summary (metrics + gate evaluation)
    - data/backtest_results/run_<timestamp>.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from src.backtester.analyze import GateThresholds, evaluate_decision_gate, print_trade_breakdown
from src.backtester.data_fetch import HistoricalDataFetcher
from src.backtester.replay import ReplayEngine
from src.clients.geckoterminal import GeckoTerminalClient
from src.config import settings
from src.core.scoring import ScoringEngine
from src.infra.cache import cache

console = Console()


async def main(sample_size: int = 50, use_cached: bool = False) -> int:
    console.print("\n[bold cyan]Solana Sniper Bot — Backtest Run[/bold cyan]\n")
    console.print(f"  Sample size: {sample_size}")
    console.print(f"  Use cached: {use_cached}")
    console.print(f"  Initial capital: 0.36 SOL")
    console.print()

    async with GeckoTerminalClient() as gecko:
        fetcher = HistoricalDataFetcher(gecko=gecko)

        # Discover candidates kalau belum di-cache
        if not use_cached:
            console.print("[cyan]Discovering candidate tokens...[/cyan]")
            candidates = await fetcher.discover_historical_token_set(sample_size=sample_size)
            console.print(f"  Found {len(candidates)} candidates")

            console.print("[cyan]Fetching historical OHLCV (skip cached)...[/cyan]")
            console.print("[dim]Free tier rate limit: ~30 req/min. ~5 menit untuk 50 token.[/dim]\n")
            dataset = await fetcher.fetch_historical_token_set(
                token_addresses=candidates,
                days=30,
                skip_cached=True,
            )
        else:
            console.print("[cyan]Loading cached dataset...[/cyan]")
            cache_dir = Path("data/backtest_cache")
            dataset = []
            for p in cache_dir.glob("*.json"):
                try:
                    dataset.append(json.loads(p.read_text()))
                except json.JSONDecodeError:
                    pass
            console.print(f"  Loaded {len(dataset)} tokens dari cache")

        if not dataset:
            console.print("[red]Dataset kosong. Tidak bisa lanjut.[/red]")
            return 1

        # Run replay
        console.print(f"\n[cyan]Replaying {len(dataset)} tokens...[/cyan]")
        engine = ReplayEngine(
            scoring=ScoringEngine(),
            initial_capital_sol=0.36,
            slippage_pct=0.05,
            fee_per_trade_sol=0.005,
        )
        result = engine.run(dataset)

        # Display metrics
        console.print("\n[bold]Backtest Metrics:[/bold]")
        for key, val in result.to_summary_dict().items():
            console.print(f"  [cyan]{key}:[/cyan] {val}")

        # Trade breakdown
        if result.trades:
            console.print(print_trade_breakdown(result))

        # Decision gate evaluation
        evaluation = evaluate_decision_gate(result)
        console.print()
        console.print(evaluation.report())

        # Save result
        output_dir = Path("data/backtest_results")
        output_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        output_path = output_dir / f"run_{ts}.json"
        output_path.write_text(
            json.dumps(
                {
                    "timestamp": ts,
                    "config": {
                        "sample_size": sample_size,
                        "use_cached": use_cached,
                        "initial_capital_sol": 0.36,
                    },
                    "metrics": result.to_summary_dict(),
                    "gate_evaluation": {
                        "passed": evaluation.passed,
                        "failures": evaluation.failures,
                    },
                    "trades": [
                        {
                            "address": t.token_address,
                            "symbol": t.symbol,
                            "entry_ts": t.entry_timestamp.isoformat(),
                            "exit_ts": t.exit_timestamp.isoformat() if t.exit_timestamp else None,
                            "entry_price_usd": t.entry_price_usd,
                            "exit_price_usd": t.exit_price_usd,
                            "entry_amount_sol": t.entry_amount_sol,
                            "exit_amount_sol": t.exit_amount_sol,
                            "entry_score": t.entry_score,
                            "pnl_sol": t.pnl_sol,
                            "pnl_pct": t.pnl_pct,
                            "won": t.won,
                            "exit_reason": t.exit_reason,
                            "holding_minutes": t.holding_minutes,
                        }
                        for t in result.trades
                    ],
                },
                indent=2,
                default=str,
            )
        )
        console.print(f"\n[green]Result saved: {output_path}[/green]")

        return 0 if evaluation.passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=50)
    parser.add_argument("--cached", action="store_true")
    args = parser.parse_args()

    try:
        exit_code = asyncio.run(main(sample_size=args.sample, use_cached=args.cached))
    finally:
        # Cleanup
        async def cleanup():
            await cache.close()
        asyncio.run(cleanup())

    sys.exit(exit_code)
