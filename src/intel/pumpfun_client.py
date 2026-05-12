"""
Pump.fun async client (public, no key required).

Pump.fun is the dominant Solana memecoin launchpad. Tokens launch on a bonding
curve, graduate to Raydium when market cap hits ~$69k.

IMPORTANT: Use frontend-api-v3.pump.fun (v3). The old frontend-api.pump.fun
returns HTTP 530 (Cloudflare origin timeout). A browser-like User-Agent header
is also REQUIRED — without it the CDN returns 530.

Base URL: https://frontend-api-v3.pump.fun
No authentication needed.
Rate limit: conservative 3 req/sec.

Bonding curve graduation:
  Market cap ~$69,000 USD triggers graduation to Raydium AMM.
  `complete` flag in API response = already graduated.
  `graduation_progress_pct` estimates 0-100 based on market_cap / 69000.

Docs: https://pump.fun (no official API docs — reverse-engineered)
"""

from __future__ import annotations

from src.clients.base import BaseHTTPClient, HTTPError
from src.config import settings
from src.infra.cache import cached
from src.infra.logger import get_logger
from src.infra.rate_limiter import TokenBucket

log = get_logger(__name__)

# Graduation threshold: bonding curve is considered "complete" at this market cap
GRADUATION_MCAP_USD: float = 69_000.0


class PumpfunClient:
    """
    Async Pump.fun client.

    Usage::

        async with PumpfunClient() as client:
            info = await client.get_token_info(mint)
            if info and client.is_in_sweet_spot(info):
                ...
    """

    def __init__(self) -> None:
        base_url = settings.pumpfun_base_url
        self._http = BaseHTTPClient(
            base_url=base_url,
            headers={
                "Accept": "application/json",
                # Browser-like UA is required — CDN rejects non-browser requests
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            },
            timeout=15.0,
            max_retries=3,
        )
        self._limiter = TokenBucket(rps=3.0, burst=10.0, name="pumpfun")

    async def close(self) -> None:
        await self._http.close()

    async def __aenter__(self) -> "PumpfunClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        await self._limiter.acquire()
        return await self._http.get(path, params=params)

    # ------------------------------------------------------------------
    # Token info
    # ------------------------------------------------------------------

    @cached(prefix="pumpfun:token", ttl=30)
    async def get_token_info(self, mint: str) -> dict | None:
        """
        Full token metadata from Pump.fun bonding curve.

        Key fields:
          complete (bool)             — True if graduated to Raydium
          market_cap (float)          — current USD market cap
          virtual_sol_reserves (int)  — SOL in bonding curve (lamports)
          virtual_token_reserves (int)— tokens in bonding curve
          total_supply (int)          — total supply (atoms)
          created_timestamp (int)     — unix ms creation time
          bonding_curve (str)         — bonding curve contract address
          name, symbol, description, image_uri

        Returns None on 404 (token not found / not a pump.fun token).

        Cached 30s — market_cap updates with every trade.
        """
        try:
            return await self._get(f"/coins/{mint}")
        except HTTPError as e:
            if e.status == 404:
                log.debug("pumpfun_token_not_found", mint=mint)
                return None
            log.error("pumpfun_token_error", mint=mint, status=e.status, error=str(e))
            return None

    # ------------------------------------------------------------------
    # Computed helpers
    # ------------------------------------------------------------------

    @staticmethod
    def graduation_progress_pct(token_info: dict) -> float:
        """
        Estimate bonding-curve graduation progress as 0-100%.

        100% means the token has (or is about to) graduate at ~$69k mcap.
        Already-graduated tokens return 100.0.

        Formula: min(market_cap / GRADUATION_MCAP_USD * 100, 100.0)
        """
        if token_info.get("complete"):
            return 100.0
        mcap = float(token_info.get("market_cap", 0) or 0)
        pct = (mcap / GRADUATION_MCAP_USD) * 100.0
        return min(pct, 100.0)

    @staticmethod
    def is_in_sweet_spot(
        token_info: dict,
        min_pct: float = 70.0,
        max_pct: float = 95.0,
    ) -> bool:
        """
        Return True if the token is in the optimal entry window.

        "Sweet spot" = deep enough in the bonding curve for momentum but
        not yet graduated (risk of liquidity evaporation on Raydium listing).

        Default window: 70-95% graduation progress.
        """
        pct = PumpfunClient.graduation_progress_pct(token_info)
        return min_pct <= pct <= max_pct

    @staticmethod
    def is_graduated(token_info: dict) -> bool:
        """Return True if token has graduated from bonding curve to Raydium."""
        return bool(token_info.get("complete", False))
