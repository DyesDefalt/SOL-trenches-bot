"""
Tests for TokitoClient (pecut-ai alternative LLM provider).

All HTTP calls are mocked. No real network I/O.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from src.ai.tokito_client import TokitoClient


class _FakeSchema(BaseModel):
    """Minimal Pydantic model for structured output tests."""
    verdict: str
    score: float


def _make_httpx_response(status_code: int = 200, json_data: dict | None = None):
    """Build a mock httpx.Response-like object."""
    mock = MagicMock()
    mock.status_code = status_code
    mock.text = json.dumps(json_data or {})
    mock.json = MagicMock(return_value=json_data or {})
    return mock


def _make_openai_response(content_dict: dict) -> dict:
    """Build an OpenAI-format chat completion response."""
    return {
        "id": "chatcmpl-test",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(content_dict),
                }
            }
        ],
        "usage": {
            "prompt_tokens": 50,
            "completion_tokens": 30,
        },
    }


class TestTokitoClientStructuredCompletion:
    """Test the main complete_structured method."""

    @pytest.mark.asyncio
    async def test_returns_parsed_pydantic_model(self):
        """Valid JSON response is parsed into Pydantic model."""
        response_data = _make_openai_response({"verdict": "safe", "score": 7.5})
        mock_response = _make_httpx_response(200, response_data)

        client = TokitoClient(api_key="test_key")
        client._client = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)

        result = await client.complete_structured(
            model="pecut-ai",
            system="You are a safety checker.",
            user="Check this token.",
            response_model=_FakeSchema,
        )

        assert result is not None
        assert result.verdict == "safe"
        assert result.score == 7.5

    @pytest.mark.asyncio
    async def test_returns_none_on_invalid_json(self):
        """Invalid JSON content → returns None (fail-safe)."""
        bad_response = {
            "choices": [
                {"message": {"role": "assistant", "content": "not json at all!"}}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        mock_response = _make_httpx_response(200, bad_response)

        client = TokitoClient(api_key="test_key")
        client._client = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)

        result = await client.complete_structured(
            model="pecut-ai",
            system="System.",
            user="User.",
            response_model=_FakeSchema,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_schema_mismatch(self):
        """JSON that doesn't match schema → returns None."""
        wrong_data = _make_openai_response({"wrong_field": "oops"})
        mock_response = _make_httpx_response(200, wrong_data)

        client = TokitoClient(api_key="test_key")
        client._client = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)

        result = await client.complete_structured(
            model="pecut-ai",
            system="System.",
            user="User.",
            response_model=_FakeSchema,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_http_error(self):
        """4xx HTTP error → returns None."""
        mock_response = _make_httpx_response(500, {"error": "internal error"})

        client = TokitoClient(api_key="test_key")
        client._client = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)

        result = await client.complete_structured(
            model="pecut-ai",
            system="System.",
            user="User.",
            response_model=_FakeSchema,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_network_exception(self):
        """Network exception → returns None (fail-safe)."""
        import httpx

        client = TokitoClient(api_key="test_key")
        client._client = MagicMock()
        client._client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

        result = await client.complete_structured(
            model="pecut-ai",
            system="System.",
            user="User.",
            response_model=_FakeSchema,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_cost_cap_exceeded(self):
        """Cost cap exceeded → skip LLM call, return None immediately."""
        client = TokitoClient(api_key="test_key")
        client._client = MagicMock()
        client._client.post = AsyncMock()

        with patch("src.ai.tokito_client.cost_tracker") as mock_tracker:
            mock_tracker.can_proceed.return_value = False

            result = await client.complete_structured(
                model="pecut-ai",
                system="System.",
                user="User.",
                response_model=_FakeSchema,
            )

        assert result is None
        client._client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_privacy_filter_applied_to_user_prompt(self):
        """PrivacyFilter.sanitize_text is applied before sending."""
        response_data = _make_openai_response({"verdict": "ok", "score": 5.0})
        mock_response = _make_httpx_response(200, response_data)

        client = TokitoClient(api_key="test_key")
        client._client = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)

        await client.complete_structured(
            model="pecut-ai",
            system="System.",
            user="Check token with api_key=supersecret",
            response_model=_FakeSchema,
        )

        call_args = client._client.post.call_args
        body = call_args[1]["json"]
        user_msg = body["messages"][1]["content"]
        # The api_key pattern should have been redacted
        assert "supersecret" not in user_msg
        assert "[REDACTED]" in user_msg

    @pytest.mark.asyncio
    async def test_always_uses_pecut_ai_model(self):
        """Regardless of model param, always sends pecut-ai to API."""
        response_data = _make_openai_response({"verdict": "ok", "score": 5.0})
        mock_response = _make_httpx_response(200, response_data)

        client = TokitoClient(api_key="test_key")
        client._client = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)

        await client.complete_structured(
            model="google/gemini-2.0-flash",   # caller passes different model
            system="System.",
            user="User.",
            response_model=_FakeSchema,
        )

        call_args = client._client.post.call_args
        body = call_args[1]["json"]
        assert body["model"] == "pecut-ai"


class TestTokitoClientContextManager:
    """Test async context manager support."""

    @pytest.mark.asyncio
    async def test_context_manager_closes_client(self):
        """__aexit__ closes the httpx client."""
        client = TokitoClient(api_key="test_key")
        client._client = MagicMock()
        client._client.aclose = AsyncMock()

        async with client:
            pass

        client._client.aclose.assert_called_once()
