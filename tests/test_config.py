"""Unit tests untuk config / settings."""

from __future__ import annotations

import os

import pytest

from src.config import Settings


def test_defaults() -> None:
    """Default values aman untuk dev (DRY_RUN=true, no real keys)."""
    # Reset env supaya pakai defaults
    s = Settings(_env_file=None)
    assert s.dry_run is True
    assert s.env == "development"
    assert s.max_position_size_sol == 0.05
    assert s.max_concurrent_positions == 2
    assert s.cb_enabled is True


def test_helius_url_construction() -> None:
    """RPC URL include API key sebagai query param."""
    os.environ["HELIUS_API_KEY"] = "test_key_123"
    s = Settings(_env_file=None)
    assert "api-key=test_key_123" in s.helius_rpc_full
    assert s.helius_rpc_full.startswith("https://")
    assert s.helius_wss_full.startswith("wss://")


def test_postgres_dsn() -> None:
    os.environ["POSTGRES_PASSWORD"] = "secret123"
    s = Settings(_env_file=None)
    assert "postgresql://" in s.postgres_dsn
    assert "secret123" in s.postgres_dsn


def test_redis_url() -> None:
    s = Settings(_env_file=None)
    assert s.redis_url.startswith("redis://")
    assert ":6379/0" in s.redis_url


def test_assert_production_ready_missing() -> None:
    """assert_production_ready return list of missing keys."""
    # Clear all required env
    for key in [
        "HELIUS_API_KEY",
        "GMGN_API_KEY",
        "WALLET_PUBLIC_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "POSTGRES_PASSWORD",
    ]:
        os.environ.pop(key, None)
    s = Settings(_env_file=None)
    missing = s.assert_production_ready()
    assert "helius_api_key" in missing
    assert "telegram_bot_token" in missing


def test_assert_production_ready_all_set() -> None:
    """Kalau semua di-set dengan nilai non-placeholder, return empty list."""
    os.environ["HELIUS_API_KEY"] = "real_helius_key"
    os.environ["GMGN_API_KEY"] = "real_gmgn_key"
    os.environ["WALLET_PUBLIC_KEY"] = "Soonsoon..."
    os.environ["TELEGRAM_BOT_TOKEN"] = "123:abc"
    os.environ["TELEGRAM_CHAT_ID"] = "987654321"
    os.environ["POSTGRES_PASSWORD"] = "strong_pwd"
    s = Settings(_env_file=None)
    missing = s.assert_production_ready()
    assert missing == []


def test_strip_trailing_slash() -> None:
    os.environ["HELIUS_RPC_URL"] = "https://mainnet.helius-rpc.com/"
    s = Settings(_env_file=None)
    assert not s.helius_rpc_url.endswith("/")
