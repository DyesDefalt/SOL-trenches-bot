"""
Nansen API async client.

Nansen provides smart-money intelligence: token screener, holder analysis,
flow intelligence, and DEX trade data with wallet labels.

Auth: `apiKey` request header (NOT Authorization).
Rate limit: 20 req/sec, 500 req/min. Honor Retry-After on 429.
Credit costs: token-screener=5, indicators=5, flow-intelligence=1,
              holders=5 (150 premium), ohlcv=1, dex-trades=1.

Set NANSEN_API_KEY env var. Set NANSEN_DAILY_CREDIT_CAP (default 300).
"""

from __future__ import annotations

import asyncio
import os
from typing import Literal

from src.clients.base import BaseHTTPClient, RateLimitError
from src.config import settings
from src.infra.cache import cached
from src.infra.logger import get_logger
from src.infra.rate_limiter import TokenBucket

log = get_logger(__name__)

TrendLabel = Literal[
    "sustained_accumulation",
    "fresh_entry",
    "reducing",
    "distribution",
    "mixed",
]


class CreditTracker:
    """
    Tracks Nansen API credit consumption against a daily cap.

    Credits are NOT persisted — resets on process restart. For production
    multi-day usage, persist to Redis or Postgres.
    """

    def __init__(self, daily_cap: int | None = None) -> None:
        self.daily_cap = daily_cap or settings.nansen_daily_credit_cap
        self._used: int = 0
        self._lock = asyncio.Lock()

    def can_proceed(self, credits_needed: int = 1) -> bool:
        """Return True if consuming `credits_needed` would not exceed daily cap."""
        return (self._used + credits_needed) <= self.daily_cap

    async def record(self, credits: int) -> None:
        """Atomically record credits consumed."""
        async with self._lock:
            self._used += credits
            if self._used >= self.daily_cap:
                log.warning(
                    "nansen_credit_cap_reached",
                    used=self._used,
                    cap=self.daily_cap,
                )

    @property
    def used(self) -> int:
        return self._used

    @property
    def remaining(self) -> int:
        return max(0, self.daily_cap - self._used)


class NansenClient:
    """
    Async Nansen API client.

    Usage::

        async with NansenClient() as client:
            tokens = await client.get_trending_tokens(chains=["solana"])
    """

    BASE_URL = "https://api.nansen.ai"

    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or settings.nansen_api_key or os.environ.get("NANSEN_API_KEY", "")
        self._http = BaseHTTPClient(
            base_url=self.BASE_URL,
            headers={
                "apiKey": key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "solana-sniper-bot/0.1",
            },
            timeout=30.0,
            max_retries=3,
        )
        # Nansen rate limits are tier-dependent; their docs example public
        # endpoints quote ~60 req/min (≈1 RPS) for the standard plans. We use
        # 1 RPS sustained with a burst of 3 to stay well under the cap and
        # avoid 429 storms during bursty scans. Bump if you have a higher
        # tier and observe headroom in NANSEN_DAILY_CREDIT_CAP usage.
        self._limiter = TokenBucket(rps=1.0, burst=3.0, name="nansen")
        self.credits = CreditTracker()

    async def close(self) -> None:
        await self._http.close()

    async def __aenter__(self) -> "NansenClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _post(
        self,
        path: str,
        body: dict,
        cost: int = 1,
    ) -> dict | None:
        """
        POST with rate limiting and credit tracking.

        Returns None (without raising) if daily credit cap is exceeded.
        """
        if not self.credits.can_proceed(cost):
            log.warning(
                "nansen_credit_cap_skip",
                path=path,
                cost=cost,
                used=self.credits.used,
                cap=self.credits.daily_cap,
            )
            return None

        await self._limiter.acquire()
        try:
            result = await self._http.post(path, json=body)
        except RateLimitError as exc:
            if exc.retry_after:
                log.info("nansen_retry_after", seconds=exc.retry_after)
                await asyncio.sleep(exc.retry_after)
            raise
        await self.credits.record(cost)
        return result

    # ------------------------------------------------------------------
    # Public API methods
    # ------------------------------------------------------------------

    @cached(prefix="nansen:trending", ttl=60)
    async def get_trending_tokens(
        self,
        chains: list[str],
        timeframe: str = "24h",
        only_smart_money: bool = True,
        limit: int = 20,
    ) -> list[dict]:
        """
        Token screener — returns tokens with smart-money activity.

        Cost: 5 credits.
        """
        body: dict = {
            "chains": chains,
            "timeframe": timeframe,
            "onlySmartMoney": only_smart_money,
            "limit": limit,
        }
        result = await self._post("/api/v1/token-screener", body, cost=5)
        if result is None:
            return []
        return result.get("data", result) if isinstance(result, dict) else result

    @cached(prefix="nansen:indicators", ttl=300)
    async def get_indicators(
        self,
        chain: str,
        token_address: str,
    ) -> dict:
        """
        Token smart-money indicators (price, volume, holder stats).

        Cost: 5 credits.
        """
        body = {"chain": chain, "tokenAddress": token_address}
        result = await self._post("/api/v1/tgm/indicators", body, cost=5)
        if result is None:
            return {}
        return result.get("data", result) if isinstance(result, dict) else {}

    @cached(prefix="nansen:flow", ttl=60)
    async def get_flow_intelligence(
        self,
        chain: str,
        token_address: str,
        timeframe: str = "1d",
    ) -> dict:
        """
        Smart-money net-flow for a specific token (1h / 1d / 7d / 30d buckets).

        Cost: 1 credit.
        """
        body = {
            "chain": chain,
            "tokenAddress": token_address,
            "timeframe": timeframe,
        }
        result = await self._post("/api/v1/tgm/flow-intelligence", body, cost=1)
        if result is None:
            return {}
        return result.get("data", result) if isinstance(result, dict) else {}

    @cached(prefix="nansen:holders", ttl=60)
    async def get_smart_money_holders(
        self,
        chain: str,
        token_address: str,
        limit: int = 20,
    ) -> list[dict]:
        """
        Smart-money holders with wallet labels.

        Cost: 5 credits (150 credits for premium labels).
        """
        body = {
            "chain": chain,
            "tokenAddress": token_address,
            "limit": limit,
        }
        result = await self._post("/api/v1/tgm/holders", body, cost=5)
        if result is None:
            return []
        if isinstance(result, dict):
            return result.get("data", result.get("holders", []))
        return result

    @cached(prefix="nansen:netflow", ttl=60)
    async def get_smart_money_netflow(
        self,
        chains: list[str],
        limit: int = 200,
    ) -> list[dict]:
        """
        Smart-money net-flow across all tokens on given chains.

        Cost: 1 credit (uses smart-money netflow endpoint — singular per API).

        Schema reference: docs.nansen.ai/api/smart-money/netflows
        - `chains` required (array of SmartMoneyChain enum values)
        - `pagination` object: { page, per_page (max 1000) }
        - DO NOT pass a bare `limit` field — server rejects with 422
          "Field 'limit' is not recognized".
        """
        # Endpoint is `/netflow` (singular). Server explicitly rejects `/netflows`
        # (plural) with a 404 hint: "Did you mean '/api/v1/smart-money/netflow'?"
        per_page = min(max(limit, 1), 1000)  # clamp to 1..1000 per API max
        body = {
            "chains": chains,
            "pagination": {"page": 1, "per_page": per_page},
        }
        result = await self._post("/api/v1/smart-money/netflow", body, cost=1)
        if result is None:
            return []
        if isinstance(result, dict):
            return result.get("data", [])
        return result

    @cached(prefix="nansen:sm_dex_trades", ttl=60)
    async def get_smart_money_dex_trades_for_token(
        self,
        chain: str,
        token_address: str,
        limit: int = 50,
    ) -> list[dict]:
        """
        Smart-money DEX trades for a specific token.

        Cost: 1 credit.
        """
        body = {
            "chain": chain,
            "tokenAddress": token_address,
            "limit": limit,
        }
        result = await self._post("/api/v1/smart-money/dex-trades", body, cost=1)
        if result is None:
            return []
        if isinstance(result, dict):
            return result.get("data", result.get("trades", []))
        return result

    @cached(prefix="nansen:ohlcv", ttl=60)
    async def get_token_ohlcv(
        self,
        chain: str,
        token_address: str,
        timeframe: str = "1h",
        from_iso: str | None = None,
        to_iso: str | None = None,
    ) -> list[dict]:
        """
        OHLCV candles for a token.

        Cost: 1 credit.
        """
        body: dict = {
            "chain": chain,
            "tokenAddress": token_address,
            "timeframe": timeframe,
        }
        if from_iso:
            body["from"] = from_iso
        if to_iso:
            body["to"] = to_iso

        result = await self._post("/api/v1/tgm/token-ohlcv", body, cost=1)
        if result is None:
            return []
        if isinstance(result, dict):
            return result.get("data", result.get("ohlcv", []))
        return result

    # ------------------------------------------------------------------
    # Interpretation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def interpret_trend(
        netflow_1h: float,
        netflow_24h: float,
        netflow_7d: float,
        netflow_30d: float,
    ) -> TrendLabel:
        """
        Smart-money trend matrix interpretation based on multi-timeframe net-flows.

        Logic:
        - sustained_accumulation: all timeframes positive (long conviction)
        - fresh_entry: only short-term (1h/24h) positive, longer TFs flat/negative
        - distribution: all TFs negative (exiting)
        - reducing: mixed, net negative leaning (7d/30d negative)
        - mixed: anything else
        """
        pos_1h = netflow_1h > 0
        pos_24h = netflow_24h > 0
        pos_7d = netflow_7d > 0
        pos_30d = netflow_30d > 0

        if pos_1h and pos_24h and pos_7d and pos_30d:
            return "sustained_accumulation"
        if pos_1h and pos_24h and not pos_7d and not pos_30d:
            return "fresh_entry"
        if not pos_1h and not pos_24h and not pos_7d and not pos_30d:
            return "distribution"
        if not pos_7d and not pos_30d:
            return "reducing"
        return "mixed"
