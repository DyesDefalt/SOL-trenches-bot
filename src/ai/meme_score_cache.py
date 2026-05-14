"""
Meme quality score cache — Phase 10.6.

Two-layer cache:
  1. In-memory dict (process-local, zero-latency hits)
  2. Redis-backed (via src.infra.cache.Cache) with TTL

Cache key: "meme_score:{token_address}"
Default TTL: settings.meme_quality_cache_ttl_seconds (300 s = 5 min)

A cache hit skips the LLM call entirely. Meme quality is relatively stable
on the minute-to-minute timescale, so 5 min TTL is safe.

Usage::

    score_cache = MemeScoreCache(cache=cache)  # cache from src.infra.cache

    hit = await score_cache.get("So1anaTokenAddr...")
    if hit is not None:
        return hit  # skip LLM

    score = await scorer.score(token_data)
    if score is not None:
        await score_cache.set("So1anaTokenAddr...", score)
"""

from __future__ import annotations

import time
from typing import Any

from src.ai.schemas import MemeQualityScore
from src.config import settings
from src.infra.logger import get_logger

log = get_logger(__name__)

_CACHE_KEY_PREFIX = "meme_score"


def _make_key(token_address: str) -> str:
    return f"{_CACHE_KEY_PREFIX}:{token_address}"


class MemeScoreCache:
    """
    In-memory + Redis-backed cache for MemeQualityScore results.

    Constructor:
        cache    — Cache instance from src.infra.cache (Redis wrapper).
                   Pass None to use in-memory only (testing, no Redis).
        ttl      — TTL seconds; defaults to settings.meme_quality_cache_ttl_seconds.
    """

    def __init__(self, cache: Any | None = None, ttl: int | None = None) -> None:
        self._redis = cache
        self._ttl = ttl if ttl is not None else settings.meme_quality_cache_ttl_seconds
        # In-memory layer: {key: (score_dict, expires_at_monotonic)}
        self._mem: dict[str, tuple[dict, float]] = {}

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def get(self, token_address: str) -> MemeQualityScore | None:
        """
        Lookup cache. Returns MemeQualityScore on hit, None on miss.

        Checks in-memory layer first; falls back to Redis if available.
        """
        key = _make_key(token_address)

        # 1. In-memory check
        mem_hit = self._mem_get(key)
        if mem_hit is not None:
            log.debug("meme_cache_mem_hit", token=token_address)
            return mem_hit

        # 2. Redis check
        if self._redis is not None:
            raw = await self._redis.get(key)
            if raw is not None:
                try:
                    score = MemeQualityScore.model_validate(raw)
                    # Populate mem layer so next hit is zero-latency
                    self._mem_set(key, raw)
                    log.debug("meme_cache_redis_hit", token=token_address)
                    return score
                except Exception as e:
                    log.warning(
                        "meme_cache_redis_decode_error",
                        token=token_address,
                        error=str(e),
                    )

        return None

    async def set(self, token_address: str, score: MemeQualityScore) -> None:
        """Store score in both in-memory and Redis layers."""
        key = _make_key(token_address)
        score_dict = score.model_dump()

        # In-memory
        self._mem_set(key, score_dict)

        # Redis
        if self._redis is not None:
            try:
                await self._redis.set(key, score_dict, ttl=self._ttl)
                log.debug("meme_cache_redis_set", token=token_address, ttl=self._ttl)
            except Exception as e:
                log.warning(
                    "meme_cache_redis_set_error",
                    token=token_address,
                    error=str(e),
                )

    async def invalidate(self, token_address: str) -> None:
        """Remove score from both layers (e.g., after token data refresh)."""
        key = _make_key(token_address)
        self._mem.pop(key, None)
        if self._redis is not None:
            try:
                await self._redis.delete(key)
            except Exception as e:
                log.warning(
                    "meme_cache_invalidate_error",
                    token=token_address,
                    error=str(e),
                )

    # ------------------------------------------------------------------
    # In-memory helpers
    # ------------------------------------------------------------------

    def _mem_get(self, key: str) -> MemeQualityScore | None:
        entry = self._mem.get(key)
        if entry is None:
            return None
        score_dict, expires_at = entry
        if time.monotonic() > expires_at:
            del self._mem[key]
            return None
        try:
            return MemeQualityScore.model_validate(score_dict)
        except Exception:
            del self._mem[key]
            return None

    def _mem_set(self, key: str, score_dict: dict) -> None:
        expires_at = time.monotonic() + self._ttl
        self._mem[key] = (score_dict, expires_at)

    def mem_size(self) -> int:
        """Return count of live (non-expired) in-memory entries."""
        now = time.monotonic()
        return sum(1 for _, (_, exp) in self._mem.items() if now <= exp)
