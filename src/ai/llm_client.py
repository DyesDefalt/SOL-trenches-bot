"""
OpenRouter LLM client dengan retry, timeout, structured output, cost tracking.

OpenRouter adalah abstraction layer yang support banyak model (Gemini, Claude, GPT, dsb.)
dengan satu API key. Endpoint: https://openrouter.ai/api/v1/chat/completions

Semua error → return None (fail-safe). Caller WAJIB handle None dengan static fallback.
"""

from __future__ import annotations

import os
from typing import Any, TypeVar

import httpx

from src.ai.cost_tracker import cost_tracker
from src.ai.privacy_filter import PrivacyFilter
from src.config import settings
from src.infra.logger import get_logger

log = get_logger(__name__)

T = TypeVar("T")

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class LLMClient:
    """
    Async OpenRouter wrapper dengan structured output support.

    Usage::

        async with LLMClient() as client:
            result = await client.complete_structured(
                model="google/gemini-2.0-flash",
                system="You are ...",
                user="Analyze this token...",
                response_model=RugCheckResult,
            )
            if result is None:
                # fallback ke static rules
                ...
    """

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = (
            api_key
            or settings.openrouter_api_key
            or os.environ.get("OPENROUTER_API_KEY", "")
        )
        self._client: httpx.AsyncClient = httpx.AsyncClient(
            timeout=settings.llm_timeout_seconds,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://solana-sniper-bot",
                "X-Title": "Solana Sniper Bot",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def complete_structured(
        self,
        model: str | None,
        system: str,
        user: str,
        response_model: type[T],
        max_tokens: int = 500,
        timeout: float = 10.0,
    ) -> T | None:
        """
        Call LLM, parse structured JSON response ke Pydantic model.

        Returns None pada:
        - Cost cap exceeded
        - Timeout
        - Invalid JSON
        - Schema validation fail
        - Any HTTP/network error

        Caller WAJIB handle None dengan static fallback.

        When `model` is None or empty, falls back to `settings.openrouter_default_model`
        (default: `openrouter/free` — auto-picks a free model with the required
        capabilities). This makes the client safe to use as a fallback target
        when the primary client's model id is unknown to OpenRouter.
        """
        # --- Resolve model: caller's choice OR safe default ---
        effective_model = (model or "").strip() or settings.openrouter_default_model

        # --- Cost cap check ---
        if not cost_tracker.can_proceed():
            log.warning("llm_skip_cost_cap", model=effective_model)
            return None

        # --- Sanitize user prompt sebelum dikirim ---
        user_clean = PrivacyFilter.sanitize_text(user)

        body: dict[str, Any] = {
            "model": effective_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_clean},
            ],
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }

        # --- Retry loop (max_retries dari settings, default 1) ---
        max_retries = max(1, settings.llm_max_retries)
        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                response = await self._client.post(
                    _OPENROUTER_URL,
                    json=body,
                    timeout=timeout,
                )

                if response.status_code == 429:
                    log.warning(
                        "llm_rate_limited",
                        model=effective_model,
                        attempt=attempt,
                        status=429,
                    )
                    if attempt < max_retries:
                        import asyncio
                        await asyncio.sleep(2 ** attempt)
                    continue

                if response.status_code >= 400:
                    log.error(
                        "llm_http_error",
                        model=effective_model,
                        status=response.status_code,
                        body=response.text[:300],
                    )
                    return None

                # --- Parse response ---
                raw = response.json()
                content_str = raw["choices"][0]["message"]["content"]

                # --- Record cost ---
                usage = raw.get("usage", {})
                input_tokens = usage.get("prompt_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0)
                cost_tracker.record(effective_model, input_tokens, output_tokens)

                # --- Parse JSON ke Pydantic model ---
                import json
                try:
                    data = json.loads(content_str)
                except json.JSONDecodeError as e:
                    log.warning(
                        "llm_json_parse_error",
                        model=effective_model,
                        error=str(e),
                        content=content_str[:200],
                    )
                    return None

                try:
                    result = response_model.model_validate(data)  # type: ignore[attr-defined]
                    log.debug(
                        "llm_complete_structured_ok",
                        model=effective_model,
                        response_model=response_model.__name__,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                    )
                    return result
                except Exception as e:
                    log.warning(
                        "llm_schema_validation_error",
                        model=effective_model,
                        response_model=response_model.__name__,
                        error=str(e),
                        data=str(data)[:300],
                    )
                    return None

            except httpx.TimeoutException as e:
                last_error = e
                log.warning(
                    "llm_timeout",
                    model=effective_model,
                    attempt=attempt,
                    timeout=timeout,
                )
                if attempt < max_retries:
                    continue
            except Exception as e:
                last_error = e
                log.error(
                    "llm_unexpected_error",
                    model=effective_model,
                    attempt=attempt,
                    error=str(e),
                )
                return None

        # Semua attempts gagal
        log.warning(
            "llm_all_attempts_failed",
            model=effective_model,
            max_retries=max_retries,
            last_error=str(last_error) if last_error else None,
        )
        return None
