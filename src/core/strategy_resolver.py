"""
Phase 10: Strategy Resolver — thin helpers for call-site migration.

Wraps StrategyManager to expose a single `get_setting()` function that
lets existing code migrate from direct `settings.X` access to DB-driven
hot-reloadable values incrementally, one key at a time.

Usage (before):
    from src.config import settings
    threshold = settings.min_score_to_buy

Usage (after, gradual migration):
    from src.core.strategy_resolver import get_setting
    threshold = await get_setting("min_score_to_buy", 75)

The `_manager` singleton is initialised lazily on first call; callers do
not need to construct or pass a StrategyManager.
"""

from __future__ import annotations

from typing import Any

from src.config import settings
from src.infra.logger import get_logger

log = get_logger(__name__)

# Module-level singleton — set by the Bot or dependency-injection.
# Can be overridden in tests via: strategy_resolver._manager = my_mock
_manager: Any = None  # type: StrategyManager | None


def set_manager(manager: Any) -> None:
    """Register the shared StrategyManager instance (called during bot init)."""
    global _manager
    _manager = manager


async def _get_manager() -> Any:
    """Lazily initialise StrategyManager with the default DB singleton."""
    global _manager
    if _manager is None:
        from src.infra.db import get_db
        from src.core.strategy_manager import StrategyManager

        db = await get_db()
        _manager = StrategyManager(db)
        log.debug("strategy_resolver_manager_auto_init")
    return _manager


async def get_setting(key: str, fallback: Any) -> Any:
    """
    Look up *key* in the active strategy config first.

    Resolution order:
      1. Active strategy config from DB (via StrategyManager / 5s cache).
      2. Attribute from `settings` (env / .env file).
      3. The provided *fallback* value.

    This allows incremental migration: call sites that switch to
    ``await get_setting(...)`` immediately get DB-driven values while
    sites that haven't been migrated yet continue to use ``settings``.

    Args:
        key:      Config key name (e.g. ``"min_score_to_buy"``).
        fallback: Value to return when the key is absent everywhere.

    Returns:
        The resolved value with its original type preserved.
    """
    try:
        manager = await _get_manager()
        config = await manager.get_active()
        if key in config:
            return config[key]
    except Exception as exc:
        log.warning("strategy_resolver_get_setting_error", key=key, error=str(exc))

    # Fallback 1: settings attribute
    env_value = getattr(settings, key, None)
    if env_value is not None:
        return env_value

    # Fallback 2: caller's default
    return fallback


async def get_active_config() -> dict[str, Any]:
    """
    Return the full active strategy config dict.

    Convenience wrapper; equivalent to ``(await _get_manager()).get_active()``.
    """
    try:
        manager = await _get_manager()
        return await manager.get_active()
    except Exception as exc:
        log.warning("strategy_resolver_get_active_config_error", error=str(exc))
        return {}
