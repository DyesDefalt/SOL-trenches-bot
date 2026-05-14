"""
Phase 10: Hot-Reloadable Strategy Manager.

Reads active trading strategy from Postgres `strategies` table with a
5-second in-memory cache. Falls back to `.env` settings when DB is
unavailable or no strategy is enabled.

Usage:
    from src.infra.db import get_db
    from src.core.strategy_manager import StrategyManager

    db = await get_db()
    sm = StrategyManager(db)
    config = await sm.get_active()
"""

from __future__ import annotations

import time
from typing import Any

from src.config import settings
from src.infra.db import Database
from src.infra.logger import get_logger

log = get_logger(__name__)

# Keys whose values come from settings and form the env fallback config.
# Order matches the strategies table config JSONB schema.
_ENV_KEYS: tuple[str, ...] = (
    "min_score_to_buy",
    "max_position_size_sol",
    "max_concurrent_positions",
    "filter_max_mcap_usd",
    "filter_min_liquidity_usd",
    "filter_min_gmgn_security_score",
    "tp1_gain_pct",
    "tp1_sell_pct",
    "tp2_gain_pct",
    "tp2_sell_pct",
    "tp3_gain_pct",
    "tp3_sell_pct",
    "hard_sl_pct",
    "trailing_stop_pct",
    "time_based_exit_minutes",
    "slippage_bps",
    "score_weight_smart_money",
    "score_weight_security",
)

# Keys that are strategy-only (no top-level settings equivalent)
_STRATEGY_ONLY_DEFAULTS: dict[str, Any] = {
    "entry_mode": "immediate",
    "max_ath_distance_pct": -10,
}


def _build_env_fallback() -> dict[str, Any]:
    """Build a config dict from current .env settings values."""
    config: dict[str, Any] = {}
    for key in _ENV_KEYS:
        config[key] = getattr(settings, key, None)
    config.update(_STRATEGY_ONLY_DEFAULTS)
    return config


def _coerce_value(key: str, value: str | float | int) -> float | int | str:
    """
    Coerce a string value to the appropriate Python type.

    Integer fields stay int, float fields stay float, string fields (like
    entry_mode) remain str.  Unknown keys are attempted as float → int
    fallback → str.
    """
    _INT_KEYS = {
        "min_score_to_buy",
        "max_concurrent_positions",
        "filter_min_gmgn_security_score",
        "time_based_exit_minutes",
        "slippage_bps",
        "score_weight_smart_money",
        "score_weight_security",
    }
    _STR_KEYS = {"entry_mode"}

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if key in _INT_KEYS:
            return int(value)
        if key in _STR_KEYS:
            return str(value)
        return value  # keep as-is (float)

    # value is a string — coerce
    str_value = str(value)
    if key in _STR_KEYS:
        return str_value
    if key in _INT_KEYS:
        try:
            return int(str_value)
        except ValueError:
            pass
    try:
        as_float = float(str_value)
        if key in _INT_KEYS:
            return int(as_float)
        # Preserve int-like floats for int keys even if passed as "75.0"
        if as_float == int(as_float):
            return int(as_float) if key in _INT_KEYS else as_float
        return as_float
    except ValueError:
        return str_value


class StrategyManager:
    """
    Manages hot-reloadable trading strategies stored in Postgres.

    - 5-second in-memory cache avoids hammering the DB on every tick.
    - Falls back to .env settings if DB is unreachable or no strategy
      is enabled.
    - All public async methods are exception-safe: DB errors are logged
      and safe defaults are returned.
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self._cache: dict[str, Any] | None = None
        self._cache_at: float = 0.0
        self._ttl: float = float(settings.strategy_cache_ttl_seconds)

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _cache_valid(self) -> bool:
        return (
            self._cache is not None
            and (time.monotonic() - self._cache_at) < self._ttl
        )

    def invalidate_cache(self) -> None:
        """Force next get_active() call to re-fetch from DB."""
        self._cache = None
        self._cache_at = 0.0
        log.debug("strategy_cache_invalidated")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_active(self) -> dict[str, Any]:
        """
        Return active strategy config dict.

        Uses 5-second in-memory cache. Falls back to .env values if
        strategy_enable_db_override=False, DB is down, or no row is
        enabled.
        """
        if not settings.strategy_enable_db_override:
            log.debug("strategy_db_override_disabled_using_env")
            return _build_env_fallback()

        if self._cache_valid():
            assert self._cache is not None
            return self._cache

        try:
            row = await self._fetch_active_row()
        except Exception as exc:
            log.warning("strategy_db_fetch_failed_fallback", error=str(exc))
            return _build_env_fallback()

        if row is None:
            log.warning("strategy_no_active_row_fallback_env")
            return _build_env_fallback()

        config: dict[str, Any] = dict(row["config"])
        self._cache = config
        self._cache_at = time.monotonic()
        log.debug("strategy_cache_refreshed", strategy_id=row["id"])
        return config

    async def list_all(self) -> list[dict[str, Any]]:
        """Return all strategies with id / name / enabled fields."""
        try:
            if self._db._pool is None:
                return []
            async with self._db._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT id, name, enabled FROM strategies ORDER BY id"
                )
            return [dict(r) for r in rows]
        except Exception as exc:
            log.warning("strategy_list_all_failed", error=str(exc))
            return []

    async def get_by_id(self, strategy_id: str) -> dict[str, Any] | None:
        """
        Return a single strategy (id, name, enabled, config) or None if
        not found.
        """
        try:
            if self._db._pool is None:
                return None
            async with self._db._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT id, name, enabled, config FROM strategies WHERE id = $1",
                    strategy_id,
                )
            if row is None:
                return None
            result = dict(row)
            result["config"] = dict(result["config"])
            return result
        except Exception as exc:
            log.warning("strategy_get_by_id_failed", strategy_id=strategy_id, error=str(exc))
            return None

    async def set_active(self, strategy_id: str) -> bool:
        """
        Enable strategy_id, disable all others atomically.

        Returns True on success, False if strategy_id not found or DB error.
        The DB trigger (enforce_single_active_strategy) also enforces this at
        the DB level; here we do an explicit transaction for clarity and to
        confirm the row exists.
        """
        try:
            if self._db._pool is None:
                return False
            async with self._db._pool.acquire() as conn:
                async with conn.transaction():
                    # Verify exists
                    exists = await conn.fetchval(
                        "SELECT 1 FROM strategies WHERE id = $1", strategy_id
                    )
                    if not exists:
                        log.warning("strategy_set_active_not_found", strategy_id=strategy_id)
                        return False
                    # Disable all
                    await conn.execute(
                        "UPDATE strategies SET enabled = FALSE, updated_at = NOW() WHERE enabled = TRUE"
                    )
                    # Enable target
                    await conn.execute(
                        "UPDATE strategies SET enabled = TRUE, updated_at = NOW() WHERE id = $1",
                        strategy_id,
                    )
            self.invalidate_cache()
            log.info("strategy_activated", strategy_id=strategy_id)
            return True
        except Exception as exc:
            log.warning("strategy_set_active_failed", strategy_id=strategy_id, error=str(exc))
            return False

    async def update_config(
        self, strategy_id: str, key: str, value: float | int | str
    ) -> bool:
        """
        Update a single JSONB key in the strategy's config.

        Type coercion is applied so Telegram string inputs map to the
        correct Python / Postgres type.  Returns True on success.
        """
        coerced = _coerce_value(key, value)
        try:
            if self._db._pool is None:
                return False
            async with self._db._pool.acquire() as conn:
                result = await conn.execute(
                    """
                    UPDATE strategies
                       SET config = jsonb_set(config, $2::text[], to_jsonb($3::text)::jsonb, true),
                           updated_at = NOW()
                     WHERE id = $1
                    """,
                    strategy_id,
                    f"{{{key}}}",
                    str(coerced),
                )
            # result is a command tag like "UPDATE 1"
            updated = result.split()[-1] != "0"
            if updated:
                self.invalidate_cache()
                log.info(
                    "strategy_config_updated",
                    strategy_id=strategy_id,
                    key=key,
                    value=coerced,
                )
            else:
                log.warning("strategy_config_update_no_rows", strategy_id=strategy_id, key=key)
            return updated
        except Exception as exc:
            log.warning(
                "strategy_update_config_failed",
                strategy_id=strategy_id,
                key=key,
                error=str(exc),
            )
            return False

    async def reset_to_defaults(self, strategy_id: str) -> bool:
        """
        Reset a strategy's config to the env-derived defaults for known keys.

        Preserves strategy-only keys (entry_mode, max_ath_distance_pct) from
        the current row so they are not silently wiped.
        """
        try:
            existing = await self.get_by_id(strategy_id)
            if existing is None:
                return False

            env_config = _build_env_fallback()
            # Merge: env keys take priority; keep any keys unique to the row
            merged = {**existing["config"], **env_config}

            if self._db._pool is None:
                return False

            import json

            async with self._db._pool.acquire() as conn:
                result = await conn.execute(
                    "UPDATE strategies SET config = $2::jsonb, updated_at = NOW() WHERE id = $1",
                    strategy_id,
                    json.dumps(merged),
                )
            updated = result.split()[-1] != "0"
            if updated:
                self.invalidate_cache()
                log.info("strategy_reset_to_defaults", strategy_id=strategy_id)
            return updated
        except Exception as exc:
            log.warning("strategy_reset_failed", strategy_id=strategy_id, error=str(exc))
            return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _fetch_active_row(self) -> Any:
        """Fetch the single enabled strategy row. Returns asyncpg Record or None."""
        if self._db._pool is None:
            raise RuntimeError("DB pool not connected")
        async with self._db._pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT id, name, config FROM strategies WHERE enabled = TRUE LIMIT 1"
            )
