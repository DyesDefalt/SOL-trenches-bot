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
    # Use the OpenAPI host. The consumer site (https://gmgn.ai) is Cloudflare-
    # protected and 403s any programmatic request. See src/clients/gmgn.py.
    # If `.env` still sets GMGN_BASE_URL=https://gmgn.ai the client overrides it.
    gmgn_base_url: str = "https://openapi.gmgn.ai"

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
    # Tuned for *frequent paper trading* — wider candidate funnel, mid-range
    # score requirement. AI rug-check + safety filters remain strict, so even
    # at score 60 we still reject scams. Adjust upward (e.g. 75+) once you
    # see false positives in paper trade outcomes.
    max_position_size_sol: float = 0.05
    max_concurrent_positions: int = 3            # was 2 — allow a third concurrent pos
    min_score_to_buy: int = 60                   # was 75 — relax for frequent trades
    min_score_to_alert: int = 45                 # was 65 — see borderline cases in TG
    slippage_bps: int = 1500
    priority_fee_microlamports: int = 10_000
    jito_tip_lamports: int = 1_000_000

    # --- Paper trading (DRY_RUN) virtual balance ---
    # When DRY_RUN=True, bot ignores the real wallet's SOL balance and uses
    # this virtual amount instead. Lets you simulate trading with any amount
    # without funding the wallet. Bot updates the virtual balance based on
    # simulated P&L during the session. Only used when DRY_RUN=True.
    # When DRY_RUN=False (live trading), the bot reads the real wallet balance.
    paper_initial_balance_sol: float = 1.0

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
    # Widened for frequent trading. Original config targeted only ultra-fresh
    # micro-caps (<$60k). Now we also allow mid-cap memes (<$500k) so the
    # scanner has a healthier candidate pool. Safety guards (security score,
    # dev holding, bundler %) are unchanged — quality remains protected.
    filter_max_mcap_usd: float = 500_000          # was 60k — mid-cap memes allowed
    filter_min_liquidity_usd: float = 5_000       # was 8k — slightly more lenient
    filter_min_gmgn_security_score: int = 70
    filter_max_dev_holding_pct: float = 15
    filter_max_bundle_supply_pct: float = 30

    # --- Nansen (Phase 7) ---
    nansen_api_key: str = Field(default="")
    nansen_base_url: str = "https://api.nansen.ai"
    nansen_daily_credit_cap: int = 300

    # --- Birdeye (Phase 7) ---
    birdeye_api_key: str = ""

    # --- CryptoQuant (Phase 9 macro) ---
    cryptoquant_api_key: str = ""
    cryptoquant_base_url: str = "https://api.cryptoquant.com/v1"

    # --- Alpha Vantage (Phase 9 macro) ---
    alphavantage_api_key: str = ""
    alphavantage_base_url: str = "https://www.alphavantage.co"

    # --- Macro regime feature flag (Phase 9) ---
    macro_regime_enabled: bool = True
    macro_regime_position_throttle_enabled: bool = True

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

    # --- News provider (Phase 9) ---
    # cryptocurrency.cv added Cloudflare BOT_BLOCKED protection — all calls
    # return 403 from VPS source IPs. DISABLED by default. Set to True only
    # if running from a residential IP or behind a residential proxy. When
    # disabled, the client returns empty results without firing any request
    # (no log spam, no rate-limit risk on a dead source).
    cryptocurrencycv_enabled: bool = False
    cryptocurrencycv_base_url: str = "https://cryptocurrency.cv"

    # --- CryptoPanic (legacy / optional fallback) ---
    # Kept for users with a paid plan. v1 was retired; v2 lives at
    # /api/{plan}/v2/ where plan ∈ {developer, growth, enterprise}. The
    # free Developer tier is being discontinued April 1, 2026. New
    # deployments should use cryptocurrency.cv instead of paying CryptoPanic.
    cryptopanic_api_key: str = ""
    cryptopanic_api_plan: str = "developer"
    cryptopanic_base_url: str = "https://cryptopanic.com/api/developer/v2"

    # --- Messari (Phase 9 cross-ref + news) ---
    # The asset profile endpoint requires Enterprise tier ($1k+/mo) in 2026.
    # DISABLED by default. Set `messari_enabled=True` and provide an
    # Enterprise key to re-enable. When disabled, client returns {} without
    # firing requests. CoinGecko cross-ref covers the same use case for free.
    messari_enabled: bool = False
    messari_api_key: str = ""
    # Host migration: data.messari.io is gone, current host is api.messari.io.
    # Paths also changed: /api/v1/assets/{slug}/profile -> /metrics/v1/assets/{slug}.
    messari_base_url: str = "https://api.messari.io"

    # --- News & narrative feature flag (Phase 9) ---
    news_narrative_enabled: bool = True
    news_fud_detection_enabled: bool = True

    # --- Health & Metrics (Phase 8) ---
    health_port: int = 8080
    metrics_enabled: bool = True

    # --- CoinGecko (Phase 9 cross-ref) ---
    coingecko_api_key: str = ""
    coingecko_base_url: str = "https://api.coingecko.com/api/v3"

    # --- Tokito alternative LLM (Phase 9) ---
    tokito_api_key: str = ""
    tokito_base_url: str = "https://api.tokito.xyz/v1"
    tokito_model: str = "pecut-ai"

    # --- OpenClaw / 9router (recommended primary LLM) ---
    # OpenAI-compatible multi-model relay (npm package `9router`, see
    # https://9router.com). `sg-combo` is a CUSTOM COMBO defined in your
    # 9router dashboard — it chains multiple providers with sticky round-robin
    # and auto-fallback. Explicit ids like `cc/claude-sonnet-4-6`,
    # `cx/gpt-5.5`, `gemini/gemini-3-flash-preview` are passed through
    # unchanged. Only use models for providers you've connected in the
    # 9router dashboard — unconfigured providers return 404
    # `no_active_credentials_for_provider`.
    # ⚠️ Default base URL is plain HTTP; switch to https:// if you re-host.
    openclaw_api_key: str = ""
    openclaw_base_url: str = "http://43.163.86.112:20128/v1"
    openclaw_default_model: str = "sg-combo"

    # --- OpenRouter default model (used as fallback) ---
    # `openrouter/free` is OpenRouter's magic router that picks a free model
    # matching the request's capability requirements (e.g. structured-output
    # support when response_format=json_object is set). Zero cost, no need to
    # maintain a list of free model ids. Replace with an explicit id like
    # `google/gemini-2.0-flash-exp:free` or `meta-llama/llama-3.2-3b-instruct:free`
    # if you want a specific free model. Use a paid id (without `:free` suffix)
    # only if OPENROUTER_API_KEY has credits.
    openrouter_default_model: str = "openrouter/free"

    # --- LLM provider selection ---
    # `llm_provider` = the PRIMARY client (Phase 9 default was openrouter).
    # `llm_fallback_provider` = the SECONDARY client tried when primary returns
    # None or raises. Set to "none" to disable fallback chaining entirely.
    # New deployments using OpenClaw should pair it with openrouter fallback:
    #   LLM_PROVIDER=openclaw
    #   LLM_FALLBACK_PROVIDER=openrouter
    # When fallback triggers, the caller's model name is REPLACED with each
    # provider's own configured default (openclaw_default_model /
    # openrouter_default_model / tokito_model) — primary-only model ids do
    # not need to be valid on the fallback provider.
    llm_provider: Literal["openrouter", "tokito", "openclaw"] = "openclaw"
    llm_fallback_provider: Literal["none", "openrouter", "tokito", "openclaw"] = "openrouter"

    # --- Cross-ref feature flag (Phase 9) ---
    crossref_validation_enabled: bool = True

    # --- Phase 10: Strategy hot-reload ---
    strategy_cache_ttl_seconds: int = 5
    strategy_enable_db_override: bool = True  # if false, always use env

    # --- Phase 10: Dip-buy mode ---
    dip_buy_default_expires_hours: int = 24
    dip_buy_check_interval_seconds: int = 30
    dip_buy_max_pending_alerts: int = 50

    # --- Phase 10.6: Meme Quality Scorer (AI) ---
    ai_meme_quality_enabled: bool = False
    meme_quality_cache_ttl_seconds: int = 300
    meme_quality_min_score_to_boost: int = 60  # below this, no bonus added
    meme_quality_score_max_bonus: float = 10.0  # max +10 to scoring engine

    # --- Phase 10.6: Fibonacci Entry Helper ---
    fib_entry_enabled: bool = False
    fib_entry_default_timeframe: str = "5m"
    fib_entry_lookback_periods: int = 100
    fib_entry_target_level: str = "0.786"
    fib_entry_min_drop_pct: float = 5.0
    fib_entry_min_swing_ratio: float = 1.5

    # --- Phase 10: Pump.fun fee-claim WebSocket listener ---
    feeclaim_enabled: bool = True
    feeclaim_min_sol: float = 0.5
    feeclaim_dedup_window_minutes: int = 10

    # --- Phase 10.5: Trader filters bundle (anti-bundler + global fee + funded-from + holder balance) ---
    trader_filters_enabled: bool = True
    trader_filters_hard_reject_enabled: bool = True  # if false, bundler/wash → score penalty only, not hard reject

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
