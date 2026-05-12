"""
GeckoTerminal API client (FREE, no key needed).

Fungsi: fallback OHLC + price data untuk Solana tokens. Mengganti Birdeye yang
mahal ($99/mo Standard).

Coverage: pools di Raydium, Orca, Meteora, Pump.fun, dll.
Rate limit free tier: ~30 req/min (tidak dipublikasikan, observed empirically).

Docs: https://www.geckoterminal.com/dex-api
"""

from __future__ import annotations

from typing import Literal

from src.clients.base import BaseHTTPClient
from src.infra.cache import cached
from src.infra.logger import get_logger
from src.infra.rate_limiter import TokenBucket

log = get_logger(__name__)

Timeframe = Literal["minute", "hour", "day"]
Aggregate = Literal[1, 5, 15, 30, 60, 240, 720]  # menit untuk minute timeframe


class GeckoTerminalClient:
    """Free OHLC + token data dari GeckoTerminal."""

    BASE_URL = "https://api.geckoterminal.com/api/v2"

    def __init__(self) -> None:
        self._http = BaseHTTPClient(
            base_url=self.BASE_URL,
            headers={
                "Accept": "application/json;version=20230302",
                "User-Agent": "solana-sniper-bot/0.1",
            },
            timeout=15.0,
            max_retries=3,
            force_ipv4=True,
        )
        # Conservative: 0.5 req/sec sustained (= 30/min)
        self._limiter = TokenBucket(rps=0.5, burst=5.0, name="geckoterminal")

    async def close(self) -> None:
        await self._http.close()

    async def __aenter__(self) -> "GeckoTerminalClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        await self._limiter.acquire()
        return await self._http.get(path, params=params)

    @cached(prefix="gecko:token", ttl=300)
    async def get_token(self, token_address: str, network: str = "solana") -> dict:
        """
        Token metadata + price + market cap.

        Returns relationship with top pools (untuk OHLC fetch berikutnya).
        """
        result = await self._get(f"/networks/{network}/tokens/{token_address}")
        return result.get("data", {})

    @cached(prefix="gecko:token_pools", ttl=300)
    async def get_token_pools(
        self,
        token_address: str,
        network: str = "solana",
        page: int = 1,
    ) -> list[dict]:
        """List of pools yang trading token ini, sorted by volume."""
        result = await self._get(
            f"/networks/{network}/tokens/{token_address}/pools",
            params={"page": page},
        )
        return result.get("data", [])

    @cached(prefix="gecko:pool", ttl=60)
    async def get_pool(self, pool_address: str, network: str = "solana") -> dict:
        """Pool details: liquidity, volume, price, base/quote tokens."""
        result = await self._get(f"/networks/{network}/pools/{pool_address}")
        return result.get("data", {})

    @cached(prefix="gecko:ohlcv", ttl=30)
    async def get_pool_ohlcv(
        self,
        pool_address: str,
        timeframe: Timeframe = "minute",
        aggregate: int = 1,
        limit: int = 100,
        before_timestamp: int | None = None,
        currency: Literal["usd", "token"] = "usd",
        network: str = "solana",
    ) -> list[list]:
        """
        OHLCV candles untuk pool.

        Returns: list of [timestamp_unix, open, high, low, close, volume_usd]

        Aggregate values:
            minute: 1, 5, 15
            hour: 1, 4, 12
            day: 1
        """
        params: dict = {
            "aggregate": aggregate,
            "limit": min(limit, 1000),
            "currency": currency,
        }
        if before_timestamp:
            params["before_timestamp"] = before_timestamp

        result = await self._get(
            f"/networks/{network}/pools/{pool_address}/ohlcv/{timeframe}",
            params=params,
        )
        # Format: data.attributes.ohlcv_list = [[ts, o, h, l, c, v], ...]
        return result.get("data", {}).get("attributes", {}).get("ohlcv_list", [])

    @cached(prefix="gecko:trending", ttl=60)
    async def get_trending_pools(
        self,
        network: str = "solana",
        page: int = 1,
        duration: Literal["5m", "1h", "6h", "24h"] = "1h",
    ) -> list[dict]:
        """Trending pools by network."""
        result = await self._get(
            f"/networks/{network}/trending_pools",
            params={"page": page, "duration": duration},
        )
        return result.get("data", [])

    @cached(prefix="gecko:new_pools", ttl=30)
    async def get_new_pools(
        self,
        network: str = "solana",
        page: int = 1,
    ) -> list[dict]:
        """New pools (recent launches). Untuk scanner low-cap."""
        result = await self._get(
            f"/networks/{network}/new_pools",
            params={"page": page},
        )
        return result.get("data", [])

    # ------------------------------------------------------------------
    # Higher-level helpers
    # ------------------------------------------------------------------
    async def get_token_top_pool(
        self,
        token_address: str,
        network: str = "solana",
    ) -> dict | None:
        """Pool dengan volume tertinggi untuk token ini. Useful untuk OHLC."""
        pools = await self.get_token_pools(token_address, network=network)
        if not pools:
            return None
        # Sort by volume_usd descending
        pools_sorted = sorted(
            pools,
            key=lambda p: float(p.get("attributes", {}).get("volume_usd", {}).get("h24", 0) or 0),
            reverse=True,
        )
        return pools_sorted[0]

    async def get_token_ohlcv(
        self,
        token_address: str,
        timeframe: Timeframe = "minute",
        aggregate: int = 5,
        limit: int = 100,
        network: str = "solana",
    ) -> list[list]:
        """
        Convenience: ambil OHLCV langsung dari token (auto-pick top pool).

        Returns: [[ts, o, h, l, c, v], ...]
        """
        top_pool = await self.get_token_top_pool(token_address, network=network)
        if not top_pool:
            log.warning("gecko_no_pool", token=token_address)
            return []

        pool_address = top_pool.get("attributes", {}).get("address")
        if not pool_address:
            return []

        return await self.get_pool_ohlcv(
            pool_address,
            timeframe=timeframe,
            aggregate=aggregate,
            limit=limit,
            network=network,
        )
