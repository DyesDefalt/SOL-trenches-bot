"""
Redis-backed async cache dengan TTL.

Digunakan agresif untuk kurangi rate-limit hit ke GMGN dan Helius free tier.

Strategi TTL default:
- Token metadata (nama, symbol, decimals): 1 jam (jarang berubah)
- Token harga + MCAP: 30 detik (volatil tapi cukup buat scanner)
- Smart wallet leaderboard: 6 jam (auto-refresh sesuai spec)
- Smart wallet trade activity: 60 detik (real-time-ish)
- OHLC candle: TTL = candle resolution (1m candle = 60s, 5m = 300s)

Usage:
    from src.infra.cache import cache, cached

    # Decorator
    @cached(prefix="gmgn:smartmoney", ttl=60)
    async def get_smart_money_trades(chain: str) -> list:
        ...

    # Manual
    await cache.set("key", value, ttl=30)
    val = await cache.get("key")
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any, ParamSpec, TypeVar

import orjson
import redis.asyncio as redis
from redis.asyncio import ConnectionPool

from src.config import settings
from src.infra.logger import get_logger

log = get_logger(__name__)

P = ParamSpec("P")
T = TypeVar("T")


class Cache:
    """Async Redis wrapper dengan JSON serialization (orjson — ~3x faster than std json)."""

    def __init__(self, url: str | None = None) -> None:
        self.url = url or settings.redis_url
        self._pool: ConnectionPool | None = None
        self._client: redis.Redis | None = None
        self._disabled = False  # set True kalau Redis tidak available — cache jadi no-op

    async def connect(self) -> None:
        """Initialize connection pool. Idempotent. Graceful: kalau Redis tak ada, mode disabled."""
        if self._client is not None or self._disabled:
            return
        self._pool = ConnectionPool.from_url(
            self.url,
            decode_responses=False,  # Kita pakai bytes (orjson)
            max_connections=20,
        )
        self._client = redis.Redis(connection_pool=self._pool)
        try:
            await self._client.ping()
            log.info("cache_connected")
        except Exception as e:
            log.warning("cache_connection_failed_disabled", error=str(e))
            self._client = None
            self._disabled = True

    async def close(self) -> None:
        """Close connection pool."""
        if self._client:
            await self._client.aclose()
            self._client = None
        if self._pool:
            await self._pool.aclose()
            self._pool = None

    async def _ensure(self) -> bool:
        """Ensure connection. Returns True kalau cache available, False kalau disabled."""
        if self._client is None and not self._disabled:
            await self.connect()
        return self._client is not None

    async def get(self, key: str) -> Any | None:
        """Get + deserialize. Returns None kalau miss atau cache disabled."""
        if not await self._ensure():
            return None
        assert self._client is not None
        try:
            raw = await self._client.get(key)
        except Exception as e:
            log.warning("cache_get_error", key=key, error=str(e))
            return None
        if raw is None:
            return None
        try:
            return orjson.loads(raw)
        except orjson.JSONDecodeError:
            log.warning("cache_decode_error", key=key)
            return None

    async def set(self, key: str, value: Any, ttl: int) -> None:
        """Serialize + set dengan TTL (detik). No-op kalau cache disabled."""
        if not await self._ensure():
            return
        assert self._client is not None
        try:
            data = orjson.dumps(value)
        except TypeError as e:
            log.warning("cache_encode_error", key=key, error=str(e))
            return
        try:
            await self._client.setex(key, ttl, data)
        except Exception as e:
            log.warning("cache_set_error", key=key, error=str(e))

    async def delete(self, *keys: str) -> int:
        """Delete keys. Returns jumlah deleted, atau 0 kalau cache disabled."""
        if not await self._ensure():
            return 0
        assert self._client is not None
        try:
            return await self._client.delete(*keys)
        except Exception as e:
            log.warning("cache_delete_error", keys=keys, error=str(e))
            return 0

    async def exists(self, key: str) -> bool:
        if not await self._ensure():
            return False
        assert self._client is not None
        try:
            return bool(await self._client.exists(key))
        except Exception:
            return False

    async def ttl(self, key: str) -> int:
        """Sisa TTL dalam detik. -1 = no expiry, -2 = not exists."""
        if not await self._ensure():
            return -2
        assert self._client is not None
        try:
            return await self._client.ttl(key)
        except Exception:
            return -2

    async def incr(self, key: str, ttl: int | None = None) -> int:
        """Atomic increment, optional set TTL kalau key baru dibuat. Returns 0 kalau disabled."""
        if not await self._ensure():
            return 0
        assert self._client is not None
        try:
            async with self._client.pipeline() as pipe:
                pipe.incr(key)
                if ttl is not None:
                    pipe.expire(key, ttl, nx=True)  # only set if not exists
                results = await pipe.execute()
            return int(results[0])
        except Exception as e:
            log.warning("cache_incr_error", key=key, error=str(e))
            return 0


# Module-level singleton
cache = Cache()


def _make_cache_key(prefix: str, args: tuple, kwargs: dict) -> str:
    """Construct deterministic cache key dari function args."""
    parts: list[str] = [prefix]
    for arg in args:
        parts.append(str(arg))
    if kwargs:
        # Sort untuk deterministik
        sorted_kwargs = sorted(kwargs.items())
        kwargs_str = json.dumps(sorted_kwargs, default=str, sort_keys=True)
        # Hash kalau panjang supaya key tetap pendek
        if len(kwargs_str) > 100:
            kwargs_str = hashlib.sha256(kwargs_str.encode()).hexdigest()[:16]
        parts.append(kwargs_str)
    return ":".join(parts)


def cached(
    prefix: str,
    ttl: int,
    skip_cache: Callable[..., bool] | None = None,
) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    """
    Decorator untuk async function: cache result di Redis.

    Args:
        prefix: cache key prefix (e.g., "gmgn:smartmoney")
        ttl: time-to-live detik
        skip_cache: optional predicate; kalau True (dapat args/kwargs), skip cache

    Example:
        @cached(prefix="gmgn:token_info", ttl=3600)
        async def get_token_info(chain: str, address: str) -> dict:
            ...
    """

    def decorator(func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            if skip_cache and skip_cache(*args, **kwargs):
                return await func(*args, **kwargs)

            # Skip 'self' kalau ini method
            cache_args = args[1:] if args and hasattr(args[0], "__class__") else args
            key = _make_cache_key(prefix, cache_args, kwargs)

            hit = await cache.get(key)
            if hit is not None:
                log.debug("cache_hit", key=key)
                return hit  # type: ignore[return-value]

            result = await func(*args, **kwargs)
            await cache.set(key, result, ttl)
            log.debug("cache_miss_set", key=key, ttl=ttl)
            return result

        return wrapper

    return decorator


# Convenience: lock untuk dedupe concurrent requests ke key yang sama
class CacheLock:
    """
    Distributed lock via Redis SETNX. Untuk dedupe concurrent fetches yang mahal.

    Usage:
        async with CacheLock(cache._client, "lock:fetch:token_xyz", ttl=10):
            # Hanya satu coroutine yang masuk sini, lainnya nunggu
            data = await fetch_expensive()
    """

    def __init__(self, key: str, ttl: int = 30) -> None:
        self.key = f"lock:{key}"
        self.ttl = ttl
        self._acquired = False

    async def __aenter__(self) -> "CacheLock":
        if cache._client is None:
            await cache.connect()
        assert cache._client is not None

        max_wait = 30
        elapsed = 0
        while elapsed < max_wait:
            acquired = await cache._client.set(self.key, "1", nx=True, ex=self.ttl)
            if acquired:
                self._acquired = True
                return self
            await asyncio.sleep(0.1)
            elapsed += 0.1
        # Timeout — fallthrough tanpa lock (fail-open, prefer correctness over duplication)
        log.warning("cache_lock_timeout", key=self.key)
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._acquired and cache._client:
            await cache._client.delete(self.key)
