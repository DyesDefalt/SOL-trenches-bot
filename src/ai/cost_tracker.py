"""
AI LLM cost tracker dengan circuit breaker.

Track daily LLM spend via OpenRouter. Auto-reset at midnight UTC.
Halt all LLM calls kalau daily cap terlampaui.

Pattern mirip CreditTracker di src/intel/nansen_client.py tapi berbasis USD bukan credits.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from src.config import settings
from src.infra.logger import get_logger

log = get_logger(__name__)

# Pricing per 1M tokens (May 2026 verified, via OpenRouter)
_PRICING: dict[str, dict[str, float]] = {
    "google/gemini-2.0-flash": {"in": 0.10, "out": 0.40},
    "google/gemini-1.5-flash": {"in": 0.075, "out": 0.30},
    "openai/gpt-4o-mini": {"in": 0.15, "out": 0.60},
    "anthropic/claude-haiku-4.5": {"in": 1.00, "out": 5.00},
    "anthropic/claude-haiku-3.5": {"in": 0.80, "out": 4.00},
    "anthropic/claude-sonnet-4.6": {"in": 3.00, "out": 15.00},
}

# Fallback pricing untuk model yang belum ada di tabel
_FALLBACK_PRICING: dict[str, float] = {"in": 1.00, "out": 5.00}


def _today_utc() -> str:
    """Return today's date as YYYY-MM-DD string, UTC."""
    return datetime.now(UTC).strftime("%Y-%m-%d")


class AICostTracker:
    """
    Track daily LLM spend, halt if cap exceeded.

    Thread-safe via asyncio.Lock. Resets at midnight UTC.
    Uses module-level singleton `cost_tracker` — tidak perlu instantiate sendiri.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._daily_spend: float = 0.0
        self._current_day: str = _today_utc()

    def _maybe_reset(self) -> None:
        """Reset spend jika hari sudah berganti (midnight UTC check)."""
        today = _today_utc()
        if today != self._current_day:
            log.info(
                "ai_cost_daily_reset",
                prev_day=self._current_day,
                prev_spend=self._daily_spend,
            )
            self._daily_spend = 0.0
            self._current_day = today

    def can_proceed(self) -> bool:
        """Return True jika masih di bawah daily cap."""
        self._maybe_reset()
        cap = settings.llm_daily_cost_cap_usd
        can = self._daily_spend < cap
        if not can:
            log.warning(
                "ai_cost_cap_exceeded",
                daily_spend=self._daily_spend,
                cap=cap,
            )
        return can

    def record(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """
        Record cost of a single LLM call. Returns cost in USD.

        Non-async karena dipanggil langsung setelah response (no I/O needed).
        Menggunakan Lock hanya untuk update — gunakan asyncio kalau perlu concurrent.
        """
        self._maybe_reset()

        pricing = _PRICING.get(model, _FALLBACK_PRICING)
        cost = (input_tokens / 1_000_000) * pricing["in"] + (
            output_tokens / 1_000_000
        ) * pricing["out"]

        self._daily_spend += cost
        log.debug(
            "ai_cost_recorded",
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=round(cost, 6),
            daily_total=round(self._daily_spend, 6),
        )
        return cost

    async def record_async(
        self, model: str, input_tokens: int, output_tokens: int
    ) -> float:
        """Thread-safe async variant untuk concurrent callers."""
        async with self._lock:
            return self.record(model, input_tokens, output_tokens)

    def daily_spend_usd(self) -> float:
        """Return current day's total spend in USD."""
        self._maybe_reset()
        return self._daily_spend

    def reset_daily(self) -> None:
        """Force reset daily spend (untuk testing / manual override)."""
        self._daily_spend = 0.0
        self._current_day = _today_utc()
        log.info("ai_cost_manual_reset")


# Module-level singleton — import dari modul lain via `from src.ai.cost_tracker import cost_tracker`
cost_tracker = AICostTracker()
