"""Tests for HealthServer HTTP endpoints."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.infra.health import HealthServer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_request() -> MagicMock:
    """Minimal aiohttp.web.Request mock."""
    return MagicMock()


async def _json_body(response) -> dict:  # type: ignore[type-arg]
    """Extract JSON from aiohttp.web.Response or a plain dict."""
    from aiohttp.web import Response

    if isinstance(response, Response):
        body = response.body
        if isinstance(body, (bytes, bytearray)):
            return json.loads(body)
        return json.loads(body)
    return response


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_returns_ok() -> None:
    """GET /health returns JSON with status='ok'."""
    server = HealthServer(port=9999, bot_ref=None)
    resp = await server._handle_health(_make_mock_request())
    data = await _json_body(resp)
    assert data["status"] == "ok"
    assert "uptime_seconds" in data
    assert data["bot_running"] is False


@pytest.mark.asyncio
async def test_health_with_bot_ref() -> None:
    """GET /health includes bot fields when bot_ref provided."""
    bot = MagicMock()
    bot._shutdown_event = MagicMock()
    bot._shutdown_event.is_set = MagicMock(return_value=False)
    bot.cb = MagicMock()
    bot.cb.state = MagicMock()
    bot.cb.state.is_paused = False
    bot.cb.state.current_balance_sol = 1.5
    bot.position_manager = MagicMock()
    bot.position_manager.open_count = 2

    server = HealthServer(port=9999, bot_ref=bot)
    resp = await server._handle_health(_make_mock_request())
    data = await _json_body(resp)

    assert data["status"] == "ok"
    assert data["bot_running"] is True
    assert data["cb_paused"] is False
    assert data["cb_balance_sol"] == 1.5
    assert data["open_positions"] == 2


# ---------------------------------------------------------------------------
# /metrics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_returns_prometheus_format() -> None:
    """GET /metrics returns Prometheus text exposition bytes."""
    server = HealthServer(port=9999)
    resp = await server._handle_metrics(_make_mock_request())

    # aiohttp Response with Prometheus content-type
    assert resp.status == 200
    body = resp.body
    if isinstance(body, (bytes, bytearray)):
        text = body.decode("utf-8")
    else:
        text = str(body)

    # Prometheus format must contain TYPE/HELP comments
    assert "# HELP" in text or "# TYPE" in text


# ---------------------------------------------------------------------------
# /ready
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ready_503_before_mark_ready() -> None:
    """GET /ready returns 503 before mark_ready() is called."""
    server = HealthServer(port=9999)
    # _ready defaults to False before start()
    server._ready = False
    resp = await server._handle_ready(_make_mock_request())
    data = await _json_body(resp)

    assert resp.status == 503
    assert data["ready"] is False


@pytest.mark.asyncio
async def test_ready_200_after_mark_ready() -> None:
    """GET /ready returns 200 after mark_ready()."""
    server = HealthServer(port=9999)
    server.mark_ready()
    resp = await server._handle_ready(_make_mock_request())
    data = await _json_body(resp)

    assert resp.status == 200
    assert data["ready"] is True
