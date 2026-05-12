"""
Refresh smart wallet registry. Jalankan via cron tiap 6 jam:

    crontab -e
    0 */6 * * * /home/bot/solana-bot/venv/bin/python /home/bot/solana-bot/scripts/refresh_smart_wallets.py >> /home/bot/solana-bot/logs/refresh.log 2>&1

Atau manual via:
    make refresh-wallets

Output: log info ke stdout, registry persisted ke data/smart_wallets.json.
"""

from __future__ import annotations

import asyncio
import sys

from rich.console import Console
from rich.table import Table

from src.clients.gmgn import GMGNClient
from src.config import settings
from src.core.smart_wallet_registry import SmartWalletRegistry

console = Console()


async def main() -> int:
    if not settings.gmgn_api_key:
        console.print("[red]ERROR: GMGN_API_KEY belum set.[/red]")
        return 1

    registry = SmartWalletRegistry()
    await registry.load()
    before = registry.stats_summary()
    console.print(f"[cyan]Loaded existing:[/cyan] {before}")

    try:
        async with GMGNClient() as gmgn:
            result = await registry.refresh(gmgn, max_age_hours=6)
    except Exception as e:
        console.print(f"[red]Refresh failed: {e}[/red]")
        return 1

    after = registry.stats_summary()

    # Show table
    table = Table(title="Smart Wallet Registry — After Refresh")
    table.add_column("Tier", style="cyan")
    table.add_column("Before", justify="right")
    table.add_column("After", justify="right")
    table.add_column("Change", justify="right")

    all_tiers = sorted(set(before.keys()) | set(after.keys()))
    for tier in all_tiers:
        b = before.get(tier, 0)
        a = after.get(tier, 0)
        change = a - b
        change_str = f"[green]+{change}[/green]" if change > 0 else f"[red]{change}[/red]" if change < 0 else "0"
        table.add_row(tier, str(b), str(a), change_str)

    console.print(table)

    # Top 10 A-tier highlights
    top_a = [w for w in registry.get_top_tier_wallets() if w.tier in ("A", "MANUAL_A")][:10]
    if top_a:
        console.print("\n[bold green]Top 10 A-Tier Wallets:[/bold green]")
        for sw in top_a:
            tier_label = sw.tier
            console.print(
                f"  [{tier_label}] {sw.address[:8]}...{sw.address[-6:]} "
                f"winrate={sw.winrate:.1%} profit={sw.realized_profit:.1f}"
            )

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
