"""
GMGN.ai API client.

Source utama untuk:
- Smart Money trades (real-time wallet activity)
- KOL trades
- Token info (security score, holders, traders)
- Trending tokens (Trenches, new launches)
- Wallet portfolio (holdings, P&L, win rate)

Auth: API Key di header. Optional Ed25519 signing untuk swap endpoints (belum dipakai
di Phase 1 — Phase 1 read-only).

Rate limit: leaky-bucket rate=10 capacity=10 dengan weight per endpoint.

CRITICAL:
- Jangan retry agresif kalau 429 — ban extend 5s per retry, max 5 menit.
- IPv4 only (GMGN tidak support IPv6).
"""

from __future__ import annotations

from typing import Any, Literal

from src.clients.base import BaseHTTPClient, HTTPError, RateLimitError
from src.config import settings
from src.infra.cache import cached
from src.infra.logger import get_logger
from src.infra.rate_limiter import LeakyBucket

log = get_logger(__name__)

Chain = Literal["sol", "bsc", "base", "eth"]
TradeSide = Literal["buy", "sell"]


# Endpoint weights per GMGN docs
ENDPOINT_WEIGHTS: dict[str, int] = {
    # Track (smart money / KOL / followed wallets)
    "/api/v1/user/smartmoney": 1,
    "/api/v1/user/kol": 1,
    "/api/v1/trade/follow_wallet": 3,
    # Portfolio
    "/api/v1/user/info": 1,
    "/api/v1/user/wallet_holdings": 2,
    "/api/v1/user/wallet_activity": 3,
    "/api/v1/user/wallet_stats": 3,
    "/api/v1/user/wallet_token_balance": 1,
    "/api/v1/user/created_tokens": 2,
}

DEFAULT_WEIGHT = 2  # konservatif untuk endpoint tak terdaftar


class GMGNClient:
    """
    Async client untuk GMGN OpenAPI.

    Usage:
        client = GMGNClient()
        await client.connect()
        trades = await client.get_smart_money_trades(chain="sol", limit=50)
        await client.close()

    Atau context manager:
        async with GMGNClient() as client:
            ...
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.api_key = api_key or settings.gmgn_api_key
        if not self.api_key:
            raise ValueError("GMGN_API_KEY not set in env")

        self.base_url = base_url or settings.gmgn_base_url

        # GMGN sits behind Cloudflare. Generic User-Agent (e.g. "solana-sniper-bot/0.1")
        # gets a 403 challenge page BEFORE the request reaches GMGN's API layer — so the
        # Bearer token is irrelevant in that case. The fix is to look like a real Chrome
        # browser at the HTTP-headers level: realistic UA + sec-ch-ua client hints +
        # Sec-Fetch-* + Origin/Referer pointing back at gmgn.ai.
        #
        # If this STILL gets blocked, Cloudflare is likely fingerprinting the TLS
        # ClientHello (JA3) — at that point swap httpx for curl_cffi with
        # `impersonate="chrome124"` which mimics Chrome at the socket level.
        self._http = BaseHTTPClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Linux"',
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "Referer": "https://gmgn.ai/",
                "Origin": "https://gmgn.ai",
            },
            timeout=30.0,
            max_retries=2,  # Conservative untuk hindari ban extension
            force_ipv4=True,
        )

        # Single shared limiter — semua endpoint share quota di GMGN
        self._limiter = LeakyBucket(rate=10.0, capacity=10.0, name="gmgn")

    async def connect(self) -> None:
        """Lazy alias for warming connection (no-op kalau sudah ready)."""
        # httpx is lazy by default, no real connect step needed

    async def close(self) -> None:
        await self._http.close()

    async def __aenter__(self) -> "GMGNClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    # ---------------------------------------------------------------------
    # Internal: rate-limited request
    # ---------------------------------------------------------------------
    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        weight = ENDPOINT_WEIGHTS.get(path, DEFAULT_WEIGHT)
        await self._limiter.acquire(weight=weight)

        try:
            return await self._http.request(method, path, params=params, json=json, retry_on_429=False)
        except RateLimitError as e:
            # GMGN ban behavior — jangan retry, log dan re-raise
            log.error(
                "gmgn_rate_limited",
                path=path,
                weight=weight,
                retry_after=e.retry_after,
                body=e.body[:200] if e.body else None,
            )
            raise

    # ---------------------------------------------------------------------
    # Smart Money / KOL tracking
    # ---------------------------------------------------------------------
    @cached(prefix="gmgn:smartmoney", ttl=60)
    async def get_smart_money_trades(
        self,
        chain: Chain = "sol",
        limit: int = 100,
        side: TradeSide | None = None,
    ) -> list[dict[str, Any]]:
        """
        Recent smart-money wallet trades.

        Note: --side filter di-apply client-side (sesuai gmgn-cli behavior).
        """
        if not 1 <= limit <= 200:
            raise ValueError("limit harus 1-200")

        result = await self._request(
            "GET",
            "/api/v1/user/smartmoney",
            params={"chain": chain, "limit": limit},
        )
        trades = result.get("data", [])
        if side:
            trades = [t for t in trades if t.get("side") == side]
        return trades

    @cached(prefix="gmgn:kol", ttl=60)
    async def get_kol_trades(
        self,
        chain: Chain = "sol",
        limit: int = 100,
        side: TradeSide | None = None,
    ) -> list[dict[str, Any]]:
        """KOL (Key Opinion Leader) wallet trades."""
        if not 1 <= limit <= 200:
            raise ValueError("limit harus 1-200")

        result = await self._request(
            "GET",
            "/api/v1/user/kol",
            params={"chain": chain, "limit": limit},
        )
        trades = result.get("data", [])
        if side:
            trades = [t for t in trades if t.get("side") == side]
        return trades

    # ---------------------------------------------------------------------
    # Wallet portfolio analysis
    # ---------------------------------------------------------------------
    @cached(prefix="gmgn:wallet_stats", ttl=300)
    async def get_wallet_stats(
        self,
        wallet: str,
        chain: Chain = "sol",
        period: Literal["7d", "30d"] = "30d",
    ) -> dict[str, Any]:
        """
        Trading stats: win rate, PnL, total profit, trade count.

        Critical untuk klasifikasi A/B/C tier smart wallet.
        """
        result = await self._request(
            "GET",
            "/api/v1/user/wallet_stats",
            params={"chain": chain, "wallet": wallet, "period": period},
        )
        return result.get("data", {})

    @cached(prefix="gmgn:wallet_holdings", ttl=120)
    async def get_wallet_holdings(
        self,
        wallet: str,
        chain: Chain = "sol",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Current token holdings + unrealized PnL."""
        result = await self._request(
            "GET",
            "/api/v1/user/wallet_holdings",
            params={"chain": chain, "wallet": wallet, "limit": limit},
        )
        return result.get("data", [])

    @cached(prefix="gmgn:wallet_activity", ttl=60)
    async def get_wallet_activity(
        self,
        wallet: str,
        chain: Chain = "sol",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Recent transaction history (buys + sells)."""
        result = await self._request(
            "GET",
            "/api/v1/user/wallet_activity",
            params={"chain": chain, "wallet": wallet, "limit": limit},
        )
        return result.get("data", [])

    # ---------------------------------------------------------------------
    # Token data
    # ---------------------------------------------------------------------
    @cached(prefix="gmgn:token_info", ttl=300)
    async def get_token_info(self, address: str, chain: Chain = "sol") -> dict[str, Any]:
        """
        Token basics + security score + pool status.

        Returns includes:
            - basic info (name, symbol, decimals)
            - market cap, liquidity, price
            - security checks (renounced, LP burned, honeypot)
            - top holders + traders
        """
        result = await self._request(
            "GET",
            "/api/v1/token/info",
            params={"chain": chain, "address": address},
        )
        return result.get("data", {})

    @cached(prefix="gmgn:trending", ttl=30)
    async def get_trending_tokens(
        self,
        chain: Chain = "sol",
        interval: Literal["1m", "5m", "1h", "6h", "24h"] = "1h",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Trending tokens — minimum 1-minute window."""
        result = await self._request(
            "GET",
            "/api/v1/market/trending",
            params={"chain": chain, "interval": interval, "limit": limit},
        )
        return result.get("data", [])

    # ---------------------------------------------------------------------
    # Higher-level helpers
    # ---------------------------------------------------------------------
    async def classify_smart_wallet(
        self,
        wallet: str,
        chain: Chain = "sol",
    ) -> Literal["A", "B", "C", "F"]:
        """
        Klasifikasi tier wallet berdasarkan stats 30 hari.

        A-Tier: win rate >= 65% AND realized profit >= 30 SOL (30d)
        B-Tier: win rate 55-64%
        C-Tier: win rate 45-54% (backup only)
        F-Tier: lainnya (skip)
        """
        try:
            stats = await self.get_wallet_stats(wallet, chain=chain, period="30d")
        except (HTTPError, RateLimitError) as e:
            log.warning("classify_wallet_failed", wallet=wallet, error=str(e))
            return "F"

        winrate = float(stats.get("winrate", 0))
        # GMGN 'pnl' field = realized_profit / total_cost multiplier
        # Realized profit dalam USD (atau native unit) — perlu cek schema actual
        realized_profit = float(stats.get("realized_profit", 0))

        if winrate >= 0.65 and realized_profit >= 30:
            return "A"
        if winrate >= 0.55:
            return "B"
        if winrate >= 0.45:
            return "C"
        return "F"

    async def get_smart_money_count_for_token(
        self,
        token_address: str,
        smart_wallets: list[str],
        chain: Chain = "sol",
        minutes_lookback: int = 15,
    ) -> tuple[int, list[str]]:
        """
        Hitung berapa smart wallet yang beli token ini dalam X menit terakhir.

        Returns: (count, list of buyer wallet addresses)

        Penting untuk scoring engine — sinyal utama (35% weight).
        """
        # Strategy: ambil recent smart money trades, filter by token + side=buy + lookback
        trades = await self.get_smart_money_trades(chain=chain, limit=200, side="buy")

        cutoff_ts = self._now_ts() - (minutes_lookback * 60)
        smart_wallet_set = set(smart_wallets)

        buyers: set[str] = set()
        for trade in trades:
            wallet = trade.get("maker_info", {}).get("address", "") or trade.get("wallet", "")
            base_address = trade.get("base_address", "")
            ts = trade.get("timestamp") or trade.get("block_time", 0)

            if not wallet or not base_address:
                continue
            if int(ts) < cutoff_ts:
                continue
            if base_address.lower() != token_address.lower():
                continue
            if wallet not in smart_wallet_set:
                continue

            buyers.add(wallet)

        return len(buyers), sorted(buyers)

    @staticmethod
    def _now_ts() -> int:
        """Current Unix timestamp (seconds)."""
        import time

        return int(time.time())
