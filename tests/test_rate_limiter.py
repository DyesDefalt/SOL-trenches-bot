"""Unit tests untuk rate limiter."""

from __future__ import annotations

import asyncio
import time

import pytest

from src.infra.rate_limiter import LeakyBucket, TokenBucket


@pytest.mark.asyncio
async def test_leaky_bucket_basic() -> None:
    """Bucket mulai full, bisa langsung acquire sampai capacity."""
    bucket = LeakyBucket(rate=10, capacity=10, name="test")
    # Habiskan capacity dengan 10 acquire weight=1
    for _ in range(10):
        await bucket.acquire(weight=1.0)
    assert bucket.available < 1.0


@pytest.mark.asyncio
async def test_leaky_bucket_refill() -> None:
    """Setelah dihabiskan, bucket refill sesuai rate."""
    bucket = LeakyBucket(rate=10, capacity=10, name="test")
    for _ in range(10):
        await bucket.acquire(weight=1.0)

    # Sleep 0.5 detik → harus refill ~5 token
    await asyncio.sleep(0.5)
    available = bucket.available
    assert 4 <= available <= 6, f"expected ~5 tokens after 0.5s, got {available}"


@pytest.mark.asyncio
async def test_leaky_bucket_weight() -> None:
    """Weight=3 menghabiskan 3x lebih banyak token."""
    bucket = LeakyBucket(rate=10, capacity=10, name="test")
    await bucket.acquire(weight=3.0)
    # Capped at capacity (10). Refill happens between calls but max=capacity.
    assert 6.5 <= bucket.available <= 10.0  # ~7 left + refill cap

    # 3 acquire weight=3 lagi total 9, harus tinggal sedikit
    for _ in range(2):
        await bucket.acquire(weight=3.0)
    assert bucket.available < 5.0  # 1 + small refill


@pytest.mark.asyncio
async def test_leaky_bucket_blocks_when_empty() -> None:
    """Acquire memblok kalau bucket empty, dan resume setelah refill."""
    bucket = LeakyBucket(rate=10, capacity=10, name="test")
    # Habiskan
    for _ in range(10):
        await bucket.acquire(weight=1.0)

    start = time.monotonic()
    await bucket.acquire(weight=1.0)  # harus tunggu ~0.1s
    elapsed = time.monotonic() - start
    assert 0.05 <= elapsed <= 0.5, f"expected ~0.1s wait, got {elapsed}"


@pytest.mark.asyncio
async def test_leaky_bucket_try_acquire_when_empty() -> None:
    """try_acquire returns False tanpa blocking kalau tidak cukup."""
    bucket = LeakyBucket(rate=10, capacity=10, name="test")
    for _ in range(10):
        await bucket.acquire(weight=1.0)

    ok = await bucket.try_acquire(weight=1.0)
    assert ok is False


@pytest.mark.asyncio
async def test_leaky_bucket_weight_too_large() -> None:
    """Weight > capacity raise ValueError."""
    bucket = LeakyBucket(rate=10, capacity=10, name="test")
    with pytest.raises(ValueError):
        await bucket.acquire(weight=11.0)


@pytest.mark.asyncio
async def test_token_bucket_rps() -> None:
    """TokenBucket(rps=5) ~ 5 req/sec sustained."""
    bucket = TokenBucket(rps=5.0, burst=5.0, name="test")
    # Habiskan burst
    for _ in range(5):
        await bucket.acquire()

    # Sustained: 5 req lagi harus tunggu ~1 detik total
    start = time.monotonic()
    for _ in range(5):
        await bucket.acquire()
    elapsed = time.monotonic() - start
    assert 0.7 <= elapsed <= 1.5, f"expected ~1.0s for 5 req at 5 rps, got {elapsed}"


@pytest.mark.asyncio
async def test_gmgn_endpoint_weights() -> None:
    """Sanity check: GMGN spec (rate=10, capacity=10) memberi sustained ~3.3 req/s untuk weight=3."""
    bucket = LeakyBucket(rate=10, capacity=10, name="gmgn")
    # 10 req weight=3 = 30 weight total
    # Capacity awal 10 = 3 req langsung jalan, 7 sisanya block sampai refill
    # Total wait = (30-10)/10 = 2 detik
    start = time.monotonic()
    for _ in range(10):
        await bucket.acquire(weight=3.0)
    elapsed = time.monotonic() - start
    # Expected ~2s, dengan tolerance
    assert 1.7 <= elapsed <= 2.5, f"expected ~2s for 10 req weight=3, got {elapsed}"
