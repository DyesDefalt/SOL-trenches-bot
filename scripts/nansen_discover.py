"""
Nansen Smart Money Discovery — pull top smart-money wallets and add to registry.

Run periodically (weekly recommended) to enrich smart wallet registry with
Nansen-labeled wallets (Fund, Smart Trader, 30D/90D Hot, KOL).

Usage:
    python scripts/nansen_discover.py [--chain solana] [--limit 200]
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from rich.console import Console
from rich.table import Table

from src.config import settings
from src.core.smart_wallet_registry import SmartWalletRegistry
from src.infra.cache import cache

console = Console()


async def main(chain: str, limit: int) -> int:
    if not settings.nansen_api_key:
        console.print("[red]ERROR: NANSEN_API_KEY tidak diset di .env[/red]")
        console.print("Daftar di https://app.nansen.ai untuk dapat API key")
        return 1

    from src.intel.nansen_client import NansenClient

    registry = SmartWalletRegistry()
    await registry.load()
    before_summary = registry.stats_summary()

    console.print(f"\n[cyan]Discovering Nansen smart money on {chain}[/cyan]")
    console.print(f"[dim]Limit: {limit} wallets[/dim]\n")

    try:
        async with NansenClient() as nansen:
            tier_counts = await registry.enrich_from_nansen(nansen)
    except Exception as e:
        console.print(f"[red]Discovery failed: {e}[/red]")
        return 1

    after_summary = registry.stats_summary()

    table = Table(title="Registry — Before vs After Nansen Discovery")
    table.add_column("Tier", style="cyan")
    table.add_column("Before", justify="right")
    table.add_column("After", justify="right")
    table.add_column("Added", justify="right", style="green")

    all_tiers = sorted(set(before_summary.keys()) | set(after_summary.keys()))
    for tier in all_tiers:
        b = before_summary.get(tier, 0)
        a = after_summary.get(tier, 0)
        delta = a - b
        delta_str = f"+{delta}" if delta > 0 else str(delta) if delta < 0 else "0"
        table.add_row(tier, str(b), str(a), delta_str)

    console.print(table)
    console.print(f"\n[green]Nansen discovery complete.[/green]")
    console.print(f"Added: {sum(tier_counts.values())} wallets")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--chain", default="solana", help="Chain (default: solana)")
    parser.add_argument("--limit", type=int, default=200, help="Max wallets to fetch")
    args = parser.parse_args()

    async def runner() -> int:
        try:
            return await main(args.chain, args.limit)
        finally:
            await cache.close()

    sys.exit(asyncio.run(runner()))
