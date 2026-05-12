"""
LLM provider selector — returns the appropriate client based on configuration.

Supported providers:
  - "openrouter" (default): OpenRouter via LLMClient
  - "tokito": Tokito (pecut-ai) via TokitoClient

Usage::

    from src.ai.llm_provider import get_llm_client

    client = get_llm_client()            # uses settings.llm_provider
    client = get_llm_client("tokito")    # explicit override
"""

from __future__ import annotations

from src.config import settings
from src.infra.logger import get_logger

log = get_logger(__name__)


def get_llm_client(provider: str | None = None):
    """
    Return an LLM client instance for the requested provider.

    Args:
        provider: "openrouter" or "tokito". If None, reads settings.llm_provider.

    Returns:
        LLMClient for "openrouter", TokitoClient for "tokito".

    Raises:
        ValueError: if provider is not a known option.
    """
    resolved = provider or settings.llm_provider

    if resolved == "tokito":
        from src.ai.tokito_client import TokitoClient
        log.debug("llm_provider_selected", provider="tokito")
        return TokitoClient()

    if resolved == "openrouter":
        from src.ai.llm_client import LLMClient
        log.debug("llm_provider_selected", provider="openrouter")
        return LLMClient()

    raise ValueError(
        f"Unknown LLM provider: {resolved!r}. "
        "Valid options: 'openrouter', 'tokito'."
    )
