"""
Tokito (pecut-ai) LLM client — OpenAI-compatible alternative to OpenRouter.

Endpoint: https://api.tokito.xyz/v1
Only model available: pecut-ai

Designed as a drop-in replacement for LLMClient with identical interface:
  - complete_structured(model, system, user, response_model, max_tokens, timeout)
  - Context manager support (__aenter__/__aexit__)

Use for:
  - Rug check when OpenRouter rate-limits
  - Cost-sensitive calls (pricing TBD for pecut-ai)
  - Fallback provider via llm_provider.py selector

All errors → return None (fail-safe). Caller WAJIB handle None.
"""

from __future__ import annotations

import json
import os
from typing import Any, TypeVar

import httpx

from src.ai.cost_tracker import cost_tracker
from src.ai.privacy_filter import PrivacyFilter
from src.config import settings
from src.infra.logger import get_logger

log = get_logger(__name__)

T = TypeVar("T")

_DEFAULT_BASE_URL = "https://api.tokito.xyz/v1"
_TOKITO_MODEL = "pecut-ai"


class TokitoClient:
    """
    Async Tokito (pecut-ai) LLM client with structured output support.

    Interface is identical to LLMClient so callers can swap providers.

    Usage::

        async with TokitoClient() as client:
            result = await client.complete_structured(
                model="pecut-ai",       # or any string — always uses pecut-ai
                system="You are ...",
                user="Analyze this token...",
                response_model=RugCheckResult,
            )
            if result is None:
                # fallback to static rules
                ...
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self._api_key = (
            api_key
            or settings.tokito_api_key
            or os.environ.get("TOKITO_API_KEY", "")
        )
        self._base_url = (
            (base_url or settings.tokito_base_url or _DEFAULT_BASE_URL).rstrip("/")
        )
        self._client: httpx.AsyncClient = httpx.AsyncClient(
            timeout=settings.llm_timeout_seconds,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "TokitoClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def complete_structured(
        self,
        model: str | None,  # accepted for interface compat — always uses pecut-ai
        system: str,
        user: str,
        response_model: type[T],
        max_tokens: int = 500,
        timeout: float = 10.0,
    ) -> T | None:
        """
        Call Tokito (pecut-ai), parse structured JSON response to Pydantic model.

        Args:
            model: Ignored — always routes to pecut-ai (single-model endpoint).
                   Kept for interface compatibility with LLMClient.

        Returns None on:
        - Cost cap exceeded
        - Timeout
        - Invalid JSON response
        - Schema validation failure
        - Any HTTP/network error

        Caller WAJIB handle None with static fallback.
        """
        # --- Cost cap check ---
        if not cost_tracker.can_proceed():
            log.warning("tokito_skip_cost_cap", requested_model=model)
            return None

        # --- Sanitize user prompt before sending ---
        user_clean = PrivacyFilter.sanitize_text(user)

        url = f"{self._base_url}/chat/completions"
        body: dict[str, Any] = {
            "model": _TOKITO_MODEL,  # single-model endpoint
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_clean},
            ],
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }

        max_retries = max(1, settings.llm_max_retries)
        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                response = await self._client.post(url, json=body, timeout=timeout)

                if response.status_code == 429:
                    log.warning(
                        "tokito_rate_limited",
                        attempt=attempt,
                        status=429,
                    )
                    if attempt < max_retries:
                        import asyncio
                        await asyncio.sleep(2 ** attempt)
                    continue

                if response.status_code >= 400:
                    log.error(
                        "tokito_http_error",
                        status=response.status_code,
                        body=response.text[:300],
                    )
                    return None

                # --- Parse response ---
                raw = response.json()
                content_str = raw["choices"][0]["message"]["content"]

                # --- Record cost (pecut-ai not in pricing table — use 0 tokens) ---
                # Note: pecut-ai pricing is not in cost_tracker._PRICING.
                # We pass 0/0 token counts so the cost cap is not double-counted
                # against a wrong fallback price. Update when Tokito publishes pricing.
                usage = raw.get("usage", {})
                input_tokens = usage.get("prompt_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0)
                # Skip cost recording for unknown-priced model to avoid fallback inflation
                # cost_tracker.record("pecut-ai", input_tokens, output_tokens)

                # --- Parse JSON to Pydantic model ---
                try:
                    data = json.loads(content_str)
                except json.JSONDecodeError as e:
                    log.warning(
                        "tokito_json_parse_error",
                        error=str(e),
                        content=content_str[:200],
                    )
                    return None

                try:
                    result = response_model.model_validate(data)  # type: ignore[attr-defined]
                    log.debug(
                        "tokito_complete_structured_ok",
                        response_model=response_model.__name__,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                    )
                    return result
                except Exception as e:
                    log.warning(
                        "tokito_schema_validation_error",
                        response_model=response_model.__name__,
                        error=str(e),
                        data=str(data)[:300],
                    )
                    return None

            except httpx.TimeoutException as e:
                last_error = e
                log.warning(
                    "tokito_timeout",
                    attempt=attempt,
                    timeout=timeout,
                )
                if attempt < max_retries:
                    continue
            except Exception as e:
                last_error = e
                log.error(
                    "tokito_unexpected_error",
                    attempt=attempt,
                    error=str(e),
                )
                return None

        log.warning(
            "tokito_all_attempts_failed",
            max_retries=max_retries,
            last_error=str(last_error) if last_error else None,
        )
        return None
