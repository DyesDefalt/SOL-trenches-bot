"""
Tests for llm_provider.get_llm_client() provider selector.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.ai.llm_provider import get_llm_client
from src.ai.llm_client import LLMClient
from src.ai.tokito_client import TokitoClient


class TestGetLlmClient:
    """Test provider selection logic."""

    def test_openrouter_explicit_returns_llm_client(self):
        """Explicit 'openrouter' → LLMClient instance."""
        client = get_llm_client("openrouter")
        assert isinstance(client, LLMClient)

    def test_tokito_explicit_returns_tokito_client(self):
        """Explicit 'tokito' → TokitoClient instance."""
        client = get_llm_client("tokito")
        assert isinstance(client, TokitoClient)

    def test_none_reads_from_settings_openrouter(self):
        """No provider arg → reads settings.llm_provider."""
        with patch("src.ai.llm_provider.settings") as mock_settings:
            mock_settings.llm_provider = "openrouter"
            client = get_llm_client(None)
        assert isinstance(client, LLMClient)

    def test_none_reads_from_settings_tokito(self):
        """No provider arg, settings=tokito → TokitoClient."""
        with patch("src.ai.llm_provider.settings") as mock_settings:
            mock_settings.llm_provider = "tokito"
            client = get_llm_client(None)
        assert isinstance(client, TokitoClient)

    def test_no_arg_reads_settings(self):
        """Calling get_llm_client() with no args uses settings default."""
        # Default settings.llm_provider = "openrouter"
        client = get_llm_client()
        assert isinstance(client, LLMClient)

    def test_unknown_provider_raises_value_error(self):
        """Unknown provider string raises ValueError."""
        with pytest.raises(ValueError, match="Unknown LLM provider"):
            get_llm_client("anthropic_direct")

    def test_provider_explicit_overrides_settings(self):
        """Explicit provider arg overrides settings.llm_provider."""
        with patch("src.ai.llm_provider.settings") as mock_settings:
            mock_settings.llm_provider = "openrouter"   # settings says openrouter
            client = get_llm_client("tokito")            # but caller says tokito
        assert isinstance(client, TokitoClient)

    def test_tokito_client_has_required_interface(self):
        """TokitoClient has same interface as LLMClient."""
        client = get_llm_client("tokito")
        assert hasattr(client, "complete_structured")
        assert hasattr(client, "close")

    def test_llm_client_has_required_interface(self):
        """LLMClient has same interface as TokitoClient."""
        client = get_llm_client("openrouter")
        assert hasattr(client, "complete_structured")
        assert hasattr(client, "close")
