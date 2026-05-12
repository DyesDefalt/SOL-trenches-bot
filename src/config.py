"""
Centralized configuration via pydantic-settings.

Loads from .env (atau secrets/.env), validates types, fail-fast kalau ada
yang missing/invalid. Import `settings` dari modul ini di file lain.

Usage:
    from src.config import settings
    print(settings.helius_api_key)
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Strongly-typed runtime config. Nilai di-load dari env vars / .env file."""

    model_config = SettingsConfigDict(
        # Cari .env di urutan ini; first hit wins
        env_file=("secrets/.env", ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Mode ---
    dry_run: bool = Field(default=True, description="Wajib true sampai backtest+smoke valid")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    env: Literal["development", "production"] = "development"

    # --- Helius ---
    helius_api_key: str = Field(default="")
    helius_rpc_url: str = "https://mainnet.helius-rpc.com"
    helius_wss_url: str = "wss://mainnet.helius-rpc.com"

    # --- GMGN ---
    gmgn_api_key: str = Field(default="")
    gmgn_private_key_path: Path | None = None
    gmgn_base_url: str = "https://gmgn.ai"

    # --- Solana Wallet ---
    wallet_path: Path | None = None
    wallet_public_key: str = Field(default="")
    solana_rpc_url: str = "https://mainnet.helius-rpc.com"

    # --- Telegram ---
    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")

    # --- Postgres ---
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "solana_bot"
    postgres_user: str = "bot"
    postgres_password: str = Field(default="")

    # --- Redis ---
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str = ""

    # --- Trading config ---
    max_position_size_sol: float = 0.05
    max_concurrent_positions: int = 2
    min_score_to_buy: int = 75
    min_score_to_alert: int = 65
    slippage_bps: int = 1500
    priority_fee_microlamports: int = 10_000
    jito_tip_lamports: int = 1_000_000

    # --- Position management ---
    tp1_gain_pct: float = 80
    tp1_sell_pct: float = 30
    tp2_gain_pct: float = 150
    tp2_sell_pct: float = 30
    tp3_gain_pct: float = 300
    tp3_sell_pct: float = 25
    hard_sl_pct: float = -45
    time_based_exit_minutes: int = 45
    trailing_stop_pct: float = 30

    # --- Circuit breaker ---
    cb_enabled: bool = True
    cb_consecutive_loss_limit: int = 3
    cb_consecutive_loss_pause_hours: int = 6
    cb_daily_loss_pct: float = 30
    cb_weekly_loss_pct: float = 50
    cb_max_drawdown_pct: float = 50
    cb_win_rate_min_pct: float = 25
    cb_win_rate_window: int = 20
    cb_drawdown_velocity_pct: float = 20
    cb_drawdown_velocity_window_minutes: int = 60

    # --- Scoring weights ---
    score_weight_smart_money: int = 35
    score_weight_mcap_position: int = 20
    score_weight_volume_momentum: int = 15
    score_weight_liquidity: int = 10
    score_weight_security: int = 10
    score_weight_kol_social: int = 5
    score_penalty_bundle: int = -10

    # --- Filter ---
    filter_max_mcap_usd: float = 60_000
    filter_min_liquidity_usd: float = 8_000
    filter_min_gmgn_security_score: int = 70
    filter_max_dev_holding_pct: float = 15
    filter_max_bundle_supply_pct: float = 30

    # --- Nansen (Phase 7) ---
    nansen_api_key: str = Field(default="")
    nansen_base_url: str = "https://api.nansen.ai"
    nansen_daily_credit_cap: int = 300

    # --- Birdeye (Phase 7) ---
    birdeye_api_key: str = ""

    # --- Pump.fun (Phase 7) ---
    pumpfun_base_url: str = "https://frontend-api-v3.pump.fun"

    # --- Intel Layer Feature Flags (Phase 7) ---
    intel_multi_source_verify_enabled: bool = True
    intel_cluster_detection_enabled: bool = True
    intel_nansen_trend_enabled: bool = True
    intel_pumpfun_tracking_enabled: bool = True

    # --- Execution Provider (Phase 7) ---
    execution_provider: Literal["jupiter", "gmgn"] = "jupiter"

    # --- AI Agent (Phase 6) ---
    ai_enabled: bool = False                    # master switch (default OFF, opt-in)
    ai_rug_check_enabled: bool = False
    ai_reflection_enabled: bool = False

    # Provider (use OpenRouter as abstraction)
    openrouter_api_key: str = Field(default="")
    llm_fast_model: str = "google/gemini-2.0-flash"           # high-volume calls
    llm_reasoning_model: str = "anthropic/claude-haiku-4.5"   # nuanced calls
    llm_premium_model: str = "anthropic/claude-sonnet-4.6"    # weekly tuner

    # Cost controls
    llm_daily_cost_cap_usd: float = 1.00
    llm_timeout_seconds: float = 10.0
    llm_max_retries: int = 1

    # Confidence thresholds
    ai_rug_veto_min_confidence: float = 0.8

    # --- Phase 6b/6c: Wallet Assessment + Dynamic Tuner ---
    ai_wallet_assessment_enabled: bool = False
    ai_tuner_enabled: bool = False
    ai_wallet_blacklist_min_confidence: float = 0.85

    # --- Health & Metrics (Phase 8) ---
    health_port: int = 8080
    metrics_enabled: bool = True

    @field_validator("helius_rpc_url", "helius_wss_url", mode="after")
    @classmethod
    def strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def helius_rpc_full(self) -> str:
        """RPC URL dengan API key sebagai query param."""
        return f"{self.helius_rpc_url}/?api-key={self.helius_api_key}"

    @property
    def helius_wss_full(self) -> str:
        """WebSocket URL dengan API key sebagai query param."""
        return f"{self.helius_wss_url}/?api-key={self.helius_api_key}"

    @property
    def postgres_dsn(self) -> str:
        """PostgreSQL DSN untuk asyncpg / SQLAlchemy."""
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        """Redis URL untuk redis-py."""
        auth = f":{self.redis_password}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"

    def assert_production_ready(self) -> list[str]:
        """
        Cek semua credential terisi untuk production. Return list of missing keys.
        Empty list = ready.
        """
        missing = []
        required = {
            "helius_api_key": self.helius_api_key,
            "gmgn_api_key": self.gmgn_api_key,
            "wallet_public_key": self.wallet_public_key,
            "telegram_bot_token": self.telegram_bot_token,
            "telegram_chat_id": self.telegram_chat_id,
            "postgres_password": self.postgres_password,
        }
        for key, value in required.items():
            if not value or value.startswith("replace_with"):
                missing.append(key)
        return missing


# Singleton instance — import dari modul lain via `from src.config import settings`
settings = Settings()
