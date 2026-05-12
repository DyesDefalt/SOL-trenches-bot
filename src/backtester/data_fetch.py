"""
Backtester data fetcher — pull historical data 30 hari untuk replay.

Sources:
- GeckoTerminal: OHLCV candles per token (free, no key)
- GMGN: smart money trades historical (rate-limited)

Output cache: data/backtest_cache/<token_address>.json

NOTE: Free tier rate limit MEMBATASI banyak. Realistic untuk backtest:
- Sample ~50-100 token (bukan 200+)
- Skip token yang fail enrichment (data incomplete)
- Run sekali, cache di disk, run ulang dari cache.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from src.infra.logger import get_logger

if TYPE_CHECKING:
    from src.clients.geckoterminal import GeckoTerminalClient
    from src.clients.gmgn import GMGNClient

log = get_logger(__name__)


CACHE_DIR = Path("data/backtest_cache")


class HistoricalDataFetcher:
    """Fetch historical token data untuk backtester replay."""

    def __init__(
        self,
        gecko: "GeckoTerminalClient",
        gmgn: "GMGNClient | None" = None,
        cache_dir: Path = CACHE_DIR,
    ) -> None:
        self.gecko = gecko
        self.gmgn = gmgn
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    async def fetch_historical_token_set(
        self,
        token_addresses: list[str],
        days: int = 30,
        candle_resolution: str = "minute",
        candle_aggregate: int = 5,
        skip_cached: bool = True,
    ) -> list[dict]:
        """
        Fetch OHLCV history + token metadata untuk batch token.

        Returns list of dicts:
            {
                "address": ..., "symbol": ..., "metadata": {...},
                "ohlcv": [[ts, o, h, l, c, v], ...]  # sorted ascending
            }
        """
        results = []
        for addr in token_addresses:
            cache_path = self.cache_dir / f"{addr}.json"
            if skip_cached and cache_path.exists():
                try:
                    cached = json.loads(cache_path.read_text())
                    results.append(cached)
                    continue
                except json.JSONDecodeError:
                    pass

            try:
                data = await self._fetch_single(addr, days, candle_resolution, candle_aggregate)
                if data:
                    cache_path.write_text(json.dumps(data))
                    results.append(data)
            except Exception as e:
                log.warning("fetch_token_failed", token=addr[:8], error=str(e))

        log.info("fetch_complete", total=len(results))
        return results

    async def _fetch_single(
        self,
        token_address: str,
        days: int,
        resolution: str,
        aggregate: int,
    ) -> dict | None:
        """Fetch metadata + OHLCV untuk satu token."""
        # 1. Token metadata
        token_data = await self.gecko.get_token(token_address)
        if not token_data:
            return None

        attrs = token_data.get("attributes", {})

        # 2. Top pool untuk OHLCV
        top_pool = await self.gecko.get_token_top_pool(token_address)
        if not top_pool:
            return None
        pool_address = top_pool.get("attributes", {}).get("address")
        if not pool_address:
            return None

        # 3. OHLCV — GeckoTerminal max 1000 candle per request
        # Untuk 30 hari × 24h × 12 (5-minute candles) = 8640 candles → perlu pagination
        # Untuk MVP: ambil 1000 candle terbaru saja (cukup untuk validasi pipeline)
        ohlcv = await self.gecko.get_pool_ohlcv(
            pool_address=pool_address,
            timeframe=resolution,
            aggregate=aggregate,
            limit=1000,
        )

        if not ohlcv:
            return None

        # OHLCV format: [[ts, o, h, l, c, v], ...] ts unix seconds
        # Sort ascending (lebih natural untuk replay)
        ohlcv_sorted = sorted(ohlcv, key=lambda x: x[0])

        return {
            "address": token_address,
            "symbol": attrs.get("symbol", "?"),
            "name": attrs.get("name", ""),
            "metadata": {
                "decimals": attrs.get("decimals", 9),
                "total_supply": attrs.get("total_supply"),
                "fdv_usd": attrs.get("fdv_usd"),
                "market_cap_usd": attrs.get("market_cap_usd"),
                "price_usd_now": attrs.get("price_usd"),
            },
            "pool_address": pool_address,
            "ohlcv": ohlcv_sorted,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    async def discover_historical_token_set(
        self,
        sample_size: int = 50,
    ) -> list[str]:
        """
        Discover candidate token untuk backtest dari new_pools + trending.

        Returns: list of token addresses.
        """
        addresses: list[str] = []
        seen: set[str] = set()

        try:
            new_pools = await self.gecko.get_new_pools()
            for pool in new_pools:
                attrs = pool.get("attributes", {})
                base = pool.get("relationships", {}).get("base_token", {}).get("data", {})
                addr = base.get("id", "").replace("solana_", "")
                if addr and addr not in seen:
                    seen.add(addr)
                    addresses.append(addr)
        except Exception as e:
            log.warning("discover_new_pools_failed", error=str(e))

        try:
            trending = await self.gecko.get_trending_pools(duration="24h")
            for pool in trending:
                base = pool.get("relationships", {}).get("base_token", {}).get("data", {})
                addr = base.get("id", "").replace("solana_", "")
                if addr and addr not in seen:
                    seen.add(addr)
                    addresses.append(addr)
        except Exception as e:
            log.warning("discover_trending_failed", error=str(e))

        return addresses[:sample_size]
