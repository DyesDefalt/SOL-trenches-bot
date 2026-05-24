"""
LLM provider selector with fallback chain.

Supported providers (set via settings.llm_provider):
  - "openclaw"   (recommended primary): OpenClaw / 9router multi-model relay
  - "openrouter" (recommended fallback): OpenRouter aggregator
  - "tokito":    pecut-ai (alternative single-model endpoint)

Fallback behaviour:
  When settings.llm_fallback_provider is set and is different from the primary,
  get_llm_client() returns a FallbackLLMClient that tries the primary first and
  falls back to the secondary on any error or None return. Caller code is
  unchanged — same complete_structured() interface.

Usage::

    from src.ai.llm_provider import get_llm_client

    async with get_llm_client() as client:
        result = await client.complete_structured(
            model=settings.llm_fast_model,  # or any provider-native model id
            system="...",
            user="...",
            response_model=MyModel,
        )
        if result is None:
            # both primary and fallback failed — use static rules
            ...
"""

from __future__ import annotations

from typing import Any, TypeVar

from src.config import settings
from src.infra.logger import get_logger

log = get_logger(__name__)

T = TypeVar("T")

_VALID_PROVIDERS = ("openrouter", "tokito", "openclaw")


def _build_client(provider: str):
    """Instantiate a raw provider client without any fallback wrapping."""
    if provider == "tokito":
        from src.ai.tokito_client import TokitoClient
        log.debug("llm_provider_selected", provider="tokito")
        return TokitoClient()

    if provider == "openrouter":
        from src.ai.llm_client import LLMClient
        log.debug("llm_provider_selected", provider="openrouter")
        return LLMClient()

    if provider == "openclaw":
        from src.ai.openclaw_client import OpenClawClient
        log.debug("llm_provider_selected", provider="openclaw")
        return OpenClawClient()

    raise ValueError(
        f"Unknown LLM provider: {provider!r}. "
        f"Valid options: {', '.join(repr(p) for p in _VALID_PROVIDERS)}."
    )


def get_llm_client(provider: str | None = None):
    """
    Return an LLM client for the requested provider, wrapped in a fallback
    chain when configured.

    Args:
        provider: Override the primary provider. If None, reads
                  settings.llm_provider. The wrapper-level fallback
                  (settings.llm_fallback_provider) is only applied when
                  `provider` is None — explicit override = no wrapping.

    Returns:
        Either a raw provider client (LLMClient / TokitoClient /
        OpenClawClient) or a FallbackLLMClient wrapping primary + fallback.
        All return types expose the same complete_structured() interface.
    """
    primary = (provider or settings.llm_provider).strip()
    primary_client = _build_client(primary)

    # Skip fallback wrapping when the caller forced a specific provider.
    if provider is not None:
        return primary_client

    fallback = (getattr(settings, "llm_fallback_provider", "none") or "none").strip()
    if fallback in ("none", "", primary):
        return primary_client

    try:
        fallback_client = _build_client(fallback)
    except ValueError as e:
        log.warning("llm_fallback_invalid", fallback=fallback, error=str(e))
        return primary_client

    log.debug("llm_fallback_active", primary=primary, fallback=fallback)
    return FallbackLLMClient(primary_client, fallback_client, primary, fallback)


class FallbackLLMClient:
    """
    Wraps two LLM clients (primary + fallback) and tries them in order.

    Same async-context-manager + complete_structured() interface as the raw
    clients so callers don't need to special-case fallback handling.

    Fallback fires on EITHER:
      - primary.complete_structured() returns None (cost cap, timeout, parse
        failure, schema mismatch, HTTP error — all signaled by None)
      - primary raises an exception (network error etc.)

    Returns None only if BOTH primary and fallback fail.
    """

    def __init__(
        self,
        primary,
        fallback,
        primary_name: str = "primary",
        fallback_name: str = "fallback",
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._primary_name = primary_name
        self._fallback_name = fallback_name

    async def __aenter__(self) -> "FallbackLLMClient":
        # Underlying clients are usable without explicit __aenter__ in the
        # existing pattern (httpx.AsyncClient is lazy), but call through in case
        # a future implementation needs setup.
        await self._primary.__aenter__()
        await self._fallback.__aenter__()
        return self

    async def __aexit__(self, *args: object) -> None:
        # Close both clients; suppress errors so the second always runs.
        try:
            await self._primary.__aexit__(*args)
        except Exception as e:  # noqa: BLE001
            log.warning("llm_fallback_primary_close_error", error=str(e))
        try:
            await self._fallback.__aexit__(*args)
        except Exception as e:  # noqa: BLE001
            log.warning("llm_fallback_secondary_close_error", error=str(e))

    async def close(self) -> None:
        # Some callers use close() instead of async-with.
        try:
            await self._primary.close()
        except Exception as e:  # noqa: BLE001
            log.warning("llm_fallback_primary_close_error", error=str(e))
        try:
            await self._fallback.close()
        except Exception as e:  # noqa: BLE001
            log.warning("llm_fallback_secondary_close_error", error=str(e))

    async def complete_structured(
        self,
        model: str,
        system: str,
        user: str,
        response_model: type[T],
        max_tokens: int = 500,
        timeout: float = 10.0,
    ) -> T | None:
        """Try primary first; on None or exception, retry with fallback."""
        # --- Primary attempt ---
        try:
            result = await self._primary.complete_structured(
                model=model,
                system=system,
                user=user,
                response_model=response_model,
                max_tokens=max_tokens,
                timeout=timeout,
            )
            if result is not None:
                return result
            log.info(
                "llm_fallback_engaging",
                reason="primary_returned_none",
                primary=self._primary_name,
                fallback=self._fallback_name,
                response_model=response_model.__name__,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "llm_fallback_engaging",
                reason="primary_raised",
                primary=self._primary_name,
                fallback=self._fallback_name,
                error=str(e),
                response_model=response_model.__name__,
            )

        # --- Fallback attempt ---
        # Pass model=None so the fallback client uses ITS OWN configured default
        # (settings.openrouter_default_model / settings.tokito_model /
        # settings.openclaw_default_model). The caller's model id may be
        # provider-specific (e.g. `sg-combo` is your private 9router combo;
        # `cc/claude-sonnet-4-6` is a 9router-prefixed id) and would 4xx on the
        # fallback. By forcing the fallback to use its own safe default
        # (OpenRouter's `openrouter/free` auto-router is the recommended one —
        # it filters for models supporting structured output and runs at $0
        # cost), the fallback path works regardless of what the caller passed.
        try:
            return await self._fallback.complete_structured(
                model=None,
                system=system,
                user=user,
                response_model=response_model,
                max_tokens=max_tokens,
                timeout=timeout,
            )
        except Exception as e:  # noqa: BLE001
            log.error(
                "llm_fallback_also_failed",
                primary=self._primary_name,
                fallback=self._fallback_name,
                error=str(e),
            )
            return None
