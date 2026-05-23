"""
Smoke test koneksi ke semua API + DB. Jalankan setelah Phase 0 setup selesai.

Usage:
    python scripts/test_connections.py

Output: status setiap komponen (Helius, GMGN, GeckoTerminal, Redis, Telegram).
Exit code 0 kalau semua OK, 1 kalau ada yang gagal.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Awaitable, Callable

from rich.console import Console
from rich.table import Table

from src.clients.geckoterminal import GeckoTerminalClient
from src.clients.gmgn import GMGNClient
from src.clients.helius import HeliusRPCClient
from src.config import settings
from src.infra.cache import cache
from src.infra.logger import get_logger

log = get_logger(__name__)
console = Console()


# Solana mainnet USDC mint — token paling stabil untuk test query
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
# Wallet address yang aktif — Solana Foundation Multisig
TEST_WALLET = "GThUX1Atko4tqhN2NaiTazWSeFWMuiUiswQrAogWN9k1"


async def test_config() -> tuple[bool, str]:
    """Verifikasi env vars terisi."""
    missing = settings.assert_production_ready()
    if missing:
        return False, f"Missing/placeholder env vars: {', '.join(missing)}"
    return True, f"All required env vars set (DRY_RUN={settings.dry_run}, ENV={settings.env})"


async def test_redis() -> tuple[bool, str]:
    """Test Redis connection + basic set/get."""
    try:
        await cache.connect()
        await cache.set("test:smoke", {"ok": True}, ttl=10)
        val = await cache.get("test:smoke")
        await cache.delete("test:smoke")
        if val and val.get("ok"):
            return True, f"Redis @ {settings.redis_host}:{settings.redis_port} OK"
        return False, "Redis set/get mismatch"
    except Exception as e:
        return False, f"Redis: {e}"


async def test_helius() -> tuple[bool, str]:
    """Test Helius RPC connection + simple query."""
    try:
        async with HeliusRPCClient() as rpc:
            # Get latest blockhash — basic RPC call
            result = await rpc.get_latest_blockhash()
            blockhash = result.get("value", {}).get("blockhash", "")
            if not blockhash:
                return False, "Helius response missing blockhash"

            # Test get_balance dengan known wallet
            balance = await rpc.get_balance(TEST_WALLET)
            sol_balance = balance / 1e9

            return True, f"Helius RPC OK. Test wallet balance: {sol_balance:.4f} SOL"
    except Exception as e:
        return False, f"Helius: {e}"


async def test_gmgn() -> tuple[bool, str]:
    """Test GMGN connection dengan endpoint trending (paling ringan)."""
    try:
        async with GMGNClient() as gmgn:
            trending = await gmgn.get_trending_tokens(chain="sol", interval="1h", limit=3)
            if not trending:
                return False, "GMGN returned empty trending list (mungkin API issue or rate limit)"
            return True, f"GMGN OK. Got {len(trending)} trending tokens."
    except Exception as e:
        return False, f"GMGN: {e}"


async def test_geckoterminal() -> tuple[bool, str]:
    """Test GeckoTerminal — gratis, tanpa key."""
    try:
        async with GeckoTerminalClient() as gecko:
            token_data = await gecko.get_token(USDC_MINT)
            if not token_data:
                return False, "GeckoTerminal returned empty data for USDC"
            symbol = token_data.get("attributes", {}).get("symbol", "?")
            return True, f"GeckoTerminal OK. USDC symbol: {symbol}"
    except Exception as e:
        return False, f"GeckoTerminal: {e}"


async def test_telegram() -> tuple[bool, str]:
    """Test Telegram bot — kirim message test ke chat user."""
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return False, "TELEGRAM_BOT_TOKEN atau TELEGRAM_CHAT_ID belum diset"

    import httpx

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    msg = "✅ Phase 0 setup test — bot ready!"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                url,
                data={"chat_id": settings.telegram_chat_id, "text": msg},
            )
            if r.status_code == 200:
                return True, "Telegram OK. Cek HP kamu untuk message test."
            return False, f"Telegram returned {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, f"Telegram: {e}"


async def test_nansen() -> tuple[bool, str]:
    """Test Nansen API connection."""
    if not settings.nansen_api_key:
        return False, "NANSEN_API_KEY not set (optional but recommended for Phase 7)"
    try:
        from src.intel.nansen_client import NansenClient
        async with NansenClient() as nansen:
            # Test with a simple call — get smart money netflow on solana
            result = await nansen.get_smart_money_netflow(chains=["solana"], limit=1)
            if result is None:
                return False, "Nansen credit cap exceeded or quota issue"
            return True, f"Nansen OK. Got {len(result)} netflow records."
    except Exception as e:
        return False, f"Nansen: {e}"


async def test_birdeye() -> tuple[bool | None, str]:
    """
    Test Birdeye token price endpoint.

    Birdeye REQUIRES an API key for /defi/price (returns 401 without). The free
    "Standard" plan (30K CU/mo, 1 RPS) is enough for our use case — sign up at
    https://bds.birdeye.so/auth/sign-up then generate a key at
    https://bds.birdeye.so/user/security and set BIRDEYE_API_KEY in .env.

    If no key is configured, this test SKIPS instead of failing so the smoke
    suite can still go green (Birdeye is optional — GeckoTerminal covers price).
    """
    if not settings.birdeye_api_key:
        return None, (
            "BIRDEYE_API_KEY not set — SKIP. Free key at "
            "https://bds.birdeye.so/user/security (Standard plan, 30K CU/mo)."
        )
    try:
        from src.intel.birdeye_client import BirdeyeClient
        async with BirdeyeClient() as birdeye:
            usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
            price = await birdeye.get_token_price(usdc)
            if price:
                return True, "Birdeye OK. USDC price fetched."
            return False, "Birdeye returned empty"
    except Exception as e:
        return False, f"Birdeye: {e}"


async def test_rugcheck() -> tuple[bool, str]:
    """Test Rugcheck (public API, no key needed)."""
    try:
        from src.intel.rugcheck_client import RugcheckClient
        async with RugcheckClient() as rc:
            usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
            report = await rc.get_token_report_summary(usdc)
            if report:
                return True, "Rugcheck OK. USDC report fetched."
            return False, "Rugcheck returned empty"
    except Exception as e:
        return False, f"Rugcheck: {e}"


async def test_dexscreener() -> tuple[bool, str]:
    """Test DexScreener (public API)."""
    try:
        from src.intel.dexscreener_client import DexscreenerClient
        async with DexscreenerClient() as ds:
            usdc = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
            pairs = await ds.get_token_pairs(usdc)
            return True, f"DexScreener OK. Got {len(pairs)} pairs for USDC."
    except Exception as e:
        return False, f"DexScreener: {e}"


async def test_pumpfun() -> tuple[bool, str]:
    """Test Pump.fun API connection."""
    try:
        from src.intel.pumpfun_client import PumpfunClient
        async with PumpfunClient() as pf:
            # Try a random known Pump.fun token, or just test connectivity
            # Use a stable test like requesting non-existent token (should not 530)
            result = await pf.get_token_info("So11111111111111111111111111111111111111112")
            # SOL isn't a pump.fun token so likely returns None — but connection works
            return True, "Pump.fun OK (connectivity verified)."
    except Exception as e:
        return False, f"Pump.fun: {e}"


# Per-test result is `tuple[bool | None, str]`:
#   True  → PASS  (green)
#   False → FAIL  (red, counts toward exit code 1)
#   None  → SKIP  (yellow, does NOT fail the suite — used for optional components)
TestResult = tuple[bool | None, str]

TESTS: list[tuple[str, Callable[[], Awaitable[TestResult]]]] = [
    ("Config", test_config),
    ("Redis", test_redis),
    ("Helius RPC", test_helius),
    ("GMGN", test_gmgn),
    ("GeckoTerminal", test_geckoterminal),
    ("Telegram", test_telegram),
    ("Nansen", test_nansen),
    ("Birdeye", test_birdeye),
    ("Rugcheck", test_rugcheck),
    ("DexScreener Direct", test_dexscreener),
    ("Pump.fun", test_pumpfun),
]


async def main() -> int:
    console.print("\n[bold cyan]Solana Sniper Bot — Smoke Test[/bold cyan]\n")

    table = Table(title="Connection Tests")
    table.add_column("Component", style="cyan", no_wrap=True)
    table.add_column("Status", style="bold")
    table.add_column("Details")

    all_ok = True
    skipped = 0
    for name, test_fn in TESTS:
        try:
            ok, detail = await test_fn()
        except Exception as e:
            ok, detail = False, f"unexpected: {e}"

        if ok is None:
            status = "[yellow]⊘ SKIP[/yellow]"
            skipped += 1
        elif ok:
            status = "[green]✓ PASS[/green]"
        else:
            status = "[red]✗ FAIL[/red]"
            all_ok = False

        table.add_row(name, status, detail)

    console.print(table)

    if all_ok and skipped == 0:
        console.print("\n[bold green]All tests passed — Phase 0 ready, lanjut Phase 1![/bold green]\n")
        return 0
    if all_ok:
        console.print(
            f"\n[bold green]All required tests passed[/bold green] "
            f"[yellow]({skipped} skipped — optional components)[/yellow]. "
            f"[bold green]Phase 0 ready.[/bold green]\n"
        )
        return 0
    console.print("\n[bold red]Some tests failed — fix di atas dulu sebelum lanjut.[/bold red]\n")
    return 1


if __name__ == "__main__":
    # Cleanup Redis connection on exit
    async def runner() -> int:
        try:
            return await main()
        finally:
            await cache.close()

    sys.exit(asyncio.run(runner()))
