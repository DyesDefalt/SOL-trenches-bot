"""
Rate limiters untuk hormati API quotas.

Dua implementasi:
1. LeakyBucket — untuk GMGN (rate=10, capacity=10, weight per endpoint)
2. TokenBucket — untuk Helius (10 req/sec free tier, sederhana fixed RPS)

Keduanya thread-safe via asyncio.Lock dan async-friendly (await acquire).

Kalau rate limit hit, acquire() tidak raise — dia await sampai slot tersedia.
Kalau benar-benar perlu fail-fast, pakai try_acquire() yang return bool.
"""

from __future__ import annotations

import asyncio
import time

from src.infra.logger import get_logger

log = get_logger(__name__)


class LeakyBucket:
    """
    Leaky bucket rate limiter.

    GMGN spec: rate=10, capacity=10, dengan weight per endpoint.
      - track smartmoney/kol = 1
      - track follow-wallet = 3
      - portfolio info = 1
      - portfolio holdings = 2
      - portfolio activity/stats = 3

    Sustained throughput = rate / weight requests/sec.
    Burst = floor(capacity / weight).

    Args:
        rate: token replenish rate (tokens per second)
        capacity: max tokens in bucket
        name: identifier untuk logging
    """

    def __init__(self, rate: float, capacity: float, name: str = "leaky_bucket") -> None:
        self.rate = rate
        self.capacity = capacity
        self.name = name
        self._tokens = capacity  # mulai full
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def _refill(self) -> None:
        """Tambahkan token sesuai elapsed time. Caller harus pegang lock."""
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._last_refill = now

    async def acquire(self, weight: float = 1.0) -> None:
        """
        Block sampai cukup token untuk weight ini.

        Kalau bucket kosong, sleep sampai cukup token (precise wait based on rate).
        """
        if weight > self.capacity:
            raise ValueError(f"weight {weight} > capacity {self.capacity}")

        async with self._lock:
            await self._refill()
            if self._tokens >= weight:
                self._tokens -= weight
                return

            # Hitung berapa lama harus tunggu sampai dapat cukup token
            deficit = weight - self._tokens
            wait_seconds = deficit / self.rate
            log.debug(
                "rate_limit_wait",
                limiter=self.name,
                weight=weight,
                deficit=deficit,
                wait_seconds=wait_seconds,
            )

        await asyncio.sleep(wait_seconds)

        async with self._lock:
            await self._refill()
            self._tokens = max(0, self._tokens - weight)

    async def try_acquire(self, weight: float = 1.0) -> bool:
        """Non-blocking. Return True kalau berhasil, False kalau tidak cukup token."""
        async with self._lock:
            await self._refill()
            if self._tokens >= weight:
                self._tokens -= weight
                return True
            return False

    @property
    def available(self) -> float:
        """Approximate tokens available saat ini (snapshot, tidak refill)."""
        elapsed = time.monotonic() - self._last_refill
        return min(self.capacity, self._tokens + elapsed * self.rate)


class TokenBucket:
    """
    Token bucket sederhana untuk fixed RPS limiter.

    Untuk Helius free tier: 10 req/sec.

    Args:
        rps: requests per second
        burst: burst capacity (default = rps)
        name: identifier
    """

    def __init__(self, rps: float, burst: float | None = None, name: str = "token_bucket") -> None:
        self.rps = rps
        self.burst = burst if burst is not None else rps
        self.name = name
        self._bucket = LeakyBucket(rate=rps, capacity=self.burst, name=name)

    async def acquire(self) -> None:
        """Block sampai 1 token tersedia."""
        await self._bucket.acquire(weight=1.0)

    async def try_acquire(self) -> bool:
        return await self._bucket.try_acquire(weight=1.0)
