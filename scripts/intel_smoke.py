"""
Intel layer smoke test — verify all Phase 7 sources working end-to-end.

Tests each source independently with a known token (USDC), reports per-source status
and timing. Use this after deploying Phase 7 to verify multi-source intelligence works.

Usage:
    python scripts/intel_smoke.py [--token <address>]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from typing import Awaitable, Callable

from rich.console import Console
from rich.table import Table

from src.config import settings
from src.infra.cache import cache
from src.infra.logger import get_logger

log = get_logger(__name__)
console = Console()

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


async def test_nansen_indicators(token: str) -> tuple[bool, str, float]:
    if not settings.nansen_api_key:
        return False, "NANSEN_API_KEY not set", 0
    try:
        from src.intel.nansen_client import NansenClient
        start = time.monotonic()
        async with NansenClient() as nc:
            indicators = await nc.get_indicators(chain="solana", token_address=token)
            elapsed = time.monotonic() - start
            if indicators is None:
                return False, "Credit cap exceeded or unavailable", elapsed
            return True, f"Got {len(indicators)} indicator fields", elapsed
    except Exception as e:
        return False, f"Error: {e}", 0


async def test_nansen_smart_money(token: str) -> tuple[bool, str, float]:
    if not settings.nansen_api_key:
        return False, "NANSEN_API_KEY not set", 0
    try:
        from src.intel.nansen_client import NansenClient
        start = time.monotonic()
        async with NansenClient() as nc:
            flow = await nc.get_flow_intelligence(chain="solana", token_address=token)
            elapsed = time.monotonic() - start
            if not flow:
                return False, "Empty flow intelligence response", elapsed
            return True, "Got flow intelligence segments", elapsed
    except Exception as e:
        return False, f"Error: {e}", 0


async def test_birdeye(token: str) -> tuple[bool, str, float]:
    try:
        from src.intel.birdeye_client import BirdeyeClient
        start = time.monotonic()
        async with BirdeyeClient() as bc:
            overview = await bc.get_token_overview(token)
            elapsed = time.monotonic() - start
            if overview:
                return True, "Got token overview", elapsed
            return False, "Empty response", elapsed
    except Exception as e:
        return False, f"Error: {e}", 0


async def test_rugcheck(token: str) -> tuple[bool, str, float]:
    try:
        from src.intel.rugcheck_client import RugcheckClient
        start = time.monotonic()
        async with RugcheckClient() as rc:
            summary = await rc.get_token_report_summary(token)
            elapsed = time.monotonic() - start
            if summary:
                safe, risks = RugcheckClient.is_safe(summary)
                return True, f"safe={safe}, risks={len(risks)}", elapsed
            return False, "Empty report", elapsed
    except Exception as e:
        return False, f"Error: {e}", 0


async def test_dexscreener(token: str) -> tuple[bool, str, float]:
    try:
        from src.intel.dexscreener_client import DexscreenerClient
        start = time.monotonic()
        async with DexscreenerClient() as ds:
            pair = await ds.get_top_pair_for_token(token)
            elapsed = time.monotonic() - start
            if pair:
                return True, f"Top pair: {pair.get('dexId', '?')}", elapsed
            return False, "No pair found", elapsed
    except Exception as e:
        return False, f"Error: {e}", 0


async def test_pumpfun(token: str) -> tuple[bool, str, float]:
    try:
        from src.intel.pumpfun_client import PumpfunClient
        start = time.monotonic()
        async with PumpfunClient() as pf:
            info = await pf.get_token_info(token)
            elapsed = time.monotonic() - start
            if info is None:
                return True, "Not a Pump.fun token (expected for USDC)", elapsed
            return True, f"graduated={info.get('complete', '?')}", elapsed
    except Exception as e:
        return False, f"Error: {e}", 0


async def test_token_verifier(token: str) -> tuple[bool, str, float]:
    """Integration test: 5-source verifier."""
    if not settings.nansen_api_key:
        return False, "Need NANSEN_API_KEY for full verifier test", 0
    try:
        from src.clients.gmgn import GMGNClient
        from src.intel.birdeye_client import BirdeyeClient
        from src.intel.dexscreener_client import DexscreenerClient
        from src.intel.nansen_client import NansenClient
        from src.intel.rugcheck_client import RugcheckClient
        from src.intel.token_verifier import TokenVerifier

        start = time.monotonic()
        async with (
            GMGNClient() as gmgn,
            NansenClient() as nansen,
            RugcheckClient() as rc,
            DexscreenerClient() as ds,
            BirdeyeClient() as be,
        ):
            verifier = TokenVerifier(
                gmgn_client=gmgn,
                nansen_client=nansen,
                rugcheck_client=rc,
                dexscreener_client=ds,
                birdeye_client=be,
            )
            result = await verifier.verify(token_address=token, chain="sol")
            elapsed = time.monotonic() - start
            return True, f"verdict={result.verdict}, score={result.weighted_safety_score:.2f}", elapsed
    except Exception as e:
        return False, f"Error: {e}", 0


TESTS: list[tuple[str, Callable[[str], Awaitable[tuple[bool, str, float]]]]] = [
    ("Nansen Indicators", test_nansen_indicators),
    ("Nansen Flow Intelligence", test_nansen_smart_money),
    ("Birdeye", test_birdeye),
    ("Rugcheck", test_rugcheck),
    ("DexScreener", test_dexscreener),
    ("Pump.fun", test_pumpfun),
    ("Token Verifier (5-source)", test_token_verifier),
]


async def main(token: str) -> int:
    console.print(f"\n[bold cyan]Intel Layer Smoke Test[/bold cyan]")
    console.print(f"Token: [yellow]{token}[/yellow]\n")

    table = Table(title="Phase 7 Multi-Source Intelligence Tests")
    table.add_column("Component", style="cyan", no_wrap=True)
    table.add_column("Status", style="bold")
    table.add_column("Detail")
    table.add_column("Latency", justify="right")

    all_ok = True
    for name, fn in TESTS:
        try:
            ok, detail, elapsed = await fn(token)
        except Exception as e:
            ok, detail, elapsed = False, f"unexpected: {e}", 0

        status = "[green]✓ PASS[/green]" if ok else "[red]✗ FAIL[/red]"
        latency = f"{elapsed*1000:.0f}ms" if elapsed > 0 else "—"
        table.add_row(name, status, detail, latency)
        if not ok:
            all_ok = False

    console.print(table)

    if all_ok:
        console.print("\n[bold green]All intel sources working![/bold green]\n")
        return 0
    console.print("\n[bold yellow]Some sources failed — see details above.[/bold yellow]\n")
    return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", default=USDC_MINT, help="Token address to test (default USDC)")
    args = parser.parse_args()

    async def runner() -> int:
        try:
            return await main(args.token)
        finally:
            await cache.close()

    sys.exit(asyncio.run(runner()))
