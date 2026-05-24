"""
OpenClaw (9router) LLM client — OpenAI-compatible router that proxies to many providers.

Endpoint default: http://43.163.86.112:20128/v1 (configurable via OPENCLAW_BASE_URL).
Authentication: Bearer token via OPENCLAW_API_KEY env var.

The router exposes 100+ models under a single API. `sg-combo` is the recommended
default — it auto-routes to the best underlying model (resolves to gpt-5.5 at
the time of writing). Explicit overrides (e.g. `cc/claude-sonnet-4-6`, `cx/gpt-5.5`,
`gemini/gemini-3.1-pro-preview`) are passed straight through.

Designed as a drop-in replacement for LLMClient with identical interface:
  - complete_structured(model, system, user, response_model, max_tokens, timeout)
  - Context manager support (__aenter__ / __aexit__)

Use for:
  - Primary LLM provider when OpenRouter quota is tight or latency is high.
  - Access to GPT-5.5 / Claude Opus 4.7 / Gemini 3 Pro via a single endpoint.
  - Fallback target wired by llm_provider.get_llm_client().

All errors → return None (fail-safe). Caller WAJIB handle None.

⚠️ Security note: the default endpoint is plain HTTP (not HTTPS). If the
deployment moves to a public-facing URL, switch OPENCLAW_BASE_URL to https://
to avoid leaking the API key on the wire.
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

_DEFAULT_BASE_URL = "http://43.163.86.112:20128/v1"
_DEFAULT_MODEL = "sg-combo"


def _model_supports_response_format(model: str) -> bool:
    """Whitelist of OpenClaw model ids known to honor `response_format`.

    The router-style ids (sg-combo, 9router/*) and most gpt-5.x flagships
    silently ignore or break on the flag — they return non-JSON bodies that
    fail outer-envelope parsing. Explicit Claude.com (cc/*) and Gemini
    (gemini/*) ids reliably honor it. When in doubt, leave the flag off and
    rely on system-prompt instructions.
    """
    if not model:
        return False
    m = model.lower()
    if m.startswith("cc/") or m.startswith("gemini/"):
        return True
    # GitHub-Copilot-proxied GPT-4/5-mini variants honor response_format.
    if m.startswith("gh/gpt-4") or m.startswith("gh/gpt-5-mini"):
        return True
    # Everything else (sg-combo, 9router/*, cx/gpt-5.x, kr/*, ag/*, etc.) — off.
    return False


class OpenClawClient:
    """
    Async OpenClaw (9router) LLM client with structured output support.

    Interface mirrors LLMClient so callers can swap providers transparently.

    Usage::

        async with OpenClawClient() as client:
            result = await client.complete_structured(
                model="sg-combo",                  # or any 9router model id
                system="You are a Solana rug-checker...",
                user="Analyze this token: ...",
                response_model=RugCheckResult,
            )
            if result is None:
                # static fallback
                ...
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        default_model: str | None = None,
    ) -> None:
        self._api_key = (
            api_key
            or settings.openclaw_api_key
            or os.environ.get("OPENCLAW_API_KEY", "")
        )
        self._base_url = (
            (base_url or settings.openclaw_base_url or _DEFAULT_BASE_URL).rstrip("/")
        )
        self._default_model = (
            default_model
            or settings.openclaw_default_model
            or _DEFAULT_MODEL
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

    async def __aenter__(self) -> "OpenClawClient":
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
        Call OpenClaw, parse structured JSON response to Pydantic model.

        Args:
            model: OpenClaw model id (e.g. "sg-combo", "cc/claude-sonnet-4-6",
                   "cx/gpt-5.5", "gemini/gemini-3.1-pro-preview"). If the caller
                   passes an OpenRouter-format name like "google/gemini-2.0-flash"
                   the request will likely 404 — pass an OpenClaw-native id, or
                   leave empty/None to use the configured default (`sg-combo`).

        Returns None on:
        - Cost cap exceeded
        - HTTP error (4xx/5xx)
        - Timeout
        - JSON parse or schema validation failure
        - Any unexpected exception

        Caller MUST handle None with static fallback.
        """
        # --- Cost cap check ---
        if not cost_tracker.can_proceed():
            log.warning("openclaw_skip_cost_cap", requested_model=model)
            return None

        # --- Sanitize user prompt before sending ---
        user_clean = PrivacyFilter.sanitize_text(user)

        # Use caller-provided model if it looks OpenClaw-shaped; otherwise default.
        # Quick heuristic: OpenClaw ids contain "/" (provider/model) OR are short
        # alias strings like "sg-combo". OpenRouter ids also use "/" so we can't
        # distinguish perfectly — accept anything truthy, fall back to default
        # when empty.
        effective_model = (model or self._default_model).strip() or _DEFAULT_MODEL

        url = f"{self._base_url}/chat/completions"
        body: dict[str, Any] = {
            "model": effective_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_clean},
            ],
            "max_tokens": max_tokens,
        }
        # Only attach response_format for models known to honor it. The
        # sg-combo router (which resolves to gpt-5.5) returns NON-JSON when
        # this flag is set, breaking outer envelope parsing. Reliable:
        # explicit cc/* (Claude.com), gemini/*, gh/gpt-4* and gh/gpt-5*-mini
        # (GitHub Copilot proxy). Unreliable: sg-combo, 9router/* aliases,
        # gpt-5.x flagship variants. The system prompt is responsible for
        # constraining output to JSON when this flag is omitted.
        if _model_supports_response_format(effective_model):
            body["response_format"] = {"type": "json_object"}

        max_retries = max(1, settings.llm_max_retries)
        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                response = await self._client.post(url, json=body, timeout=timeout)

                if response.status_code == 429:
                    log.warning(
                        "openclaw_rate_limited",
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
                        "openclaw_http_error",
                        model=effective_model,
                        status=response.status_code,
                        body=response.text[:300],
                    )
                    return None

                # --- Parse OUTER envelope (the OpenAI {choices: [...]} wrapper) ---
                # If the server returned non-JSON (e.g. plain "OK" string when a
                # model ignores response_format), response.json() raises
                # json.JSONDecodeError. Surface the actual body so we can debug.
                try:
                    raw = response.json()
                except json.JSONDecodeError as e:
                    log.warning(
                        "openclaw_envelope_not_json",
                        model=effective_model,
                        error=str(e),
                        status=response.status_code,
                        content_type=response.headers.get("content-type", "?"),
                        body_preview=response.text[:300],
                    )
                    return None

                try:
                    content_str = raw["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError) as e:
                    log.warning(
                        "openclaw_response_shape_unexpected",
                        model=effective_model,
                        error=str(e),
                        raw_preview=str(raw)[:300],
                    )
                    return None

                # --- Record cost: OpenClaw resolves dynamically to an underlying
                # model (sg-combo → gpt-5.5 at write time). Pricing per underlying
                # model is uncertain, so we skip cost_tracker.record() to avoid
                # double-counting the daily cap with the wrong unit price.
                # Update once OpenClaw publishes per-model pricing.
                usage = raw.get("usage", {})
                input_tokens = usage.get("prompt_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0)
                # cost_tracker.record(effective_model, input_tokens, output_tokens)

                # --- Parse JSON to Pydantic model ---
                try:
                    data = json.loads(content_str)
                except json.JSONDecodeError as e:
                    log.warning(
                        "openclaw_json_parse_error",
                        model=effective_model,
                        error=str(e),
                        content=content_str[:200],
                    )
                    return None

                try:
                    result = response_model.model_validate(data)  # type: ignore[attr-defined]
                    log.debug(
                        "openclaw_complete_structured_ok",
                        model=effective_model,
                        response_model=response_model.__name__,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                    )
                    return result
                except Exception as e:
                    log.warning(
                        "openclaw_schema_validation_error",
                        model=effective_model,
                        response_model=response_model.__name__,
                        error=str(e),
                        data=str(data)[:300],
                    )
                    return None

            except httpx.TimeoutException as e:
                last_error = e
                log.warning(
                    "openclaw_timeout",
                    model=effective_model,
                    attempt=attempt,
                    timeout=timeout,
                )
                if attempt < max_retries:
                    continue
            except Exception as e:
                last_error = e
                log.error(
                    "openclaw_unexpected_error",
                    model=effective_model,
                    attempt=attempt,
                    error=str(e),
                )
                return None

        log.warning(
            "openclaw_all_attempts_failed",
            model=effective_model,
            max_retries=max_retries,
            last_error=str(last_error) if last_error else None,
        )
        return None
