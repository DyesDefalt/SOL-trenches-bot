"""
CLI untuk manage smart wallet registry secara manual.

Usage:
    # Tambah wallet manual (override auto classification)
    python scripts/manage_smart_wallets.py add <address> --tier A --notes "alpha guy from twitter"

    # Blacklist wallet
    python scripts/manage_smart_wallets.py blacklist <address> --notes "wash trader"

    # List semua active
    python scripts/manage_smart_wallets.py list

    # List top tier saja (yang ditrack via WS)
    python scripts/manage_smart_wallets.py list --top

    # Stats
    python scripts/manage_smart_wallets.py stats

    # Bootstrap pertama kali (full discovery dari GMGN)
    python scripts/manage_smart_wallets.py bootstrap --sample 200
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from rich.console import Console
from rich.table import Table

from src.clients.gmgn import GMGNClient
from src.config import settings
from src.core.smart_wallet_registry import SmartWalletRegistry

console = Console()


async def cmd_list(top_only: bool = False) -> int:
    registry = SmartWalletRegistry()
    await registry.load()

    wallets = registry.get_top_tier_wallets() if top_only else registry.get_active_wallets()
    if not wallets:
        console.print("[yellow]Registry kosong. Run `bootstrap` dulu.[/yellow]")
        return 0

    table = Table(title=f"Smart Wallets ({'top tier only' if top_only else 'all active'})")
    table.add_column("Tier", style="cyan")
    table.add_column("Address")
    table.add_column("Winrate", justify="right")
    table.add_column("Profit", justify="right")
    table.add_column("Trades", justify="right")
    table.add_column("Source")
    table.add_column("Notes")

    for sw in wallets:
        addr = f"{sw.address[:6]}...{sw.address[-4:]}"
        table.add_row(
            sw.tier,
            addr,
            f"{sw.winrate:.1%}" if sw.winrate else "-",
            f"{sw.realized_profit:.1f}" if sw.realized_profit else "-",
            str(sw.buy_count + sw.sell_count) if sw.buy_count else "-",
            sw.source,
            sw.notes[:30] if sw.notes else "",
        )
    console.print(table)
    return 0


async def cmd_add(address: str, tier: str, notes: str) -> int:
    registry = SmartWalletRegistry()
    await registry.load()
    registry.add_manual(address=address, tier=tier, notes=notes)  # type: ignore[arg-type]
    console.print(f"[green]✓ Added {address} as MANUAL_{tier}[/green]")
    if notes:
        console.print(f"  Notes: {notes}")
    return 0


async def cmd_blacklist(address: str, notes: str) -> int:
    registry = SmartWalletRegistry()
    await registry.load()
    registry.add_blacklist(address=address, notes=notes)
    console.print(f"[red]✗ Blacklisted {address}[/red]")
    if notes:
        console.print(f"  Notes: {notes}")
    return 0


async def cmd_stats() -> int:
    registry = SmartWalletRegistry()
    await registry.load()
    summary = registry.stats_summary()
    if not summary:
        console.print("[yellow]Registry kosong.[/yellow]")
        return 0

    table = Table(title="Smart Wallet Registry Stats")
    table.add_column("Tier")
    table.add_column("Count", justify="right")

    total_active = 0
    total_top = 0
    for tier, count in sorted(summary.items()):
        table.add_row(tier, str(count))
        if tier in ("A", "B", "C", "MANUAL_A", "MANUAL_B"):
            total_active += count
        if tier in ("A", "B", "MANUAL_A", "MANUAL_B"):
            total_top += count

    console.print(table)
    console.print(f"\n[cyan]Total active (A+B+C):[/cyan] {total_active}")
    console.print(f"[green]Top tier (A+B, untuk WS):[/green] {total_top}")
    return 0


async def cmd_bootstrap(sample: int) -> int:
    if not settings.gmgn_api_key:
        console.print("[red]ERROR: GMGN_API_KEY belum set di secrets/.env[/red]")
        return 1

    registry = SmartWalletRegistry()
    await registry.load()
    console.print(f"[cyan]Bootstrap dari GMGN, sample size={sample}...[/cyan]")
    console.print("[dim]Ini butuh ~30-60 detik tergantung free-tier rate limit.[/dim]\n")

    try:
        async with GMGNClient() as gmgn:
            result = await registry.bootstrap_from_gmgn(gmgn, sample_size=sample)
    except Exception as e:
        console.print(f"[red]Bootstrap failed: {e}[/red]")
        return 1

    table = Table(title="Bootstrap Results")
    table.add_column("Tier")
    table.add_column("Count", justify="right")
    for tier, count in sorted(result.items()):
        table.add_row(tier, str(count))
    console.print(table)
    console.print(f"\n[green]Saved to {registry.registry_path}[/green]")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage smart wallet registry")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List wallets")
    p_list.add_argument("--top", action="store_true", help="Top tier only (A+B)")

    p_add = sub.add_parser("add", help="Add wallet manually")
    p_add.add_argument("address", help="Wallet address (case-insensitive)")
    p_add.add_argument("--tier", choices=["A", "B"], default="A")
    p_add.add_argument("--notes", default="")

    p_bl = sub.add_parser("blacklist", help="Blacklist wallet")
    p_bl.add_argument("address")
    p_bl.add_argument("--notes", default="")

    sub.add_parser("stats", help="Show registry stats")

    p_boot = sub.add_parser("bootstrap", help="Full discovery dari GMGN")
    p_boot.add_argument("--sample", type=int, default=200)

    args = parser.parse_args()

    if args.cmd == "list":
        return asyncio.run(cmd_list(top_only=args.top))
    if args.cmd == "add":
        return asyncio.run(cmd_add(args.address, args.tier, args.notes))
    if args.cmd == "blacklist":
        return asyncio.run(cmd_blacklist(args.address, args.notes))
    if args.cmd == "stats":
        return asyncio.run(cmd_stats())
    if args.cmd == "bootstrap":
        return asyncio.run(cmd_bootstrap(args.sample))

    return 1


if __name__ == "__main__":
    sys.exit(main())
