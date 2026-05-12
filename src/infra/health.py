"""
Health server — lightweight HTTP server for liveness/readiness probes
and Prometheus metrics scraping.

Listens on 127.0.0.1:{settings.health_port} (default 8080) using aiohttp.

Endpoints:
    GET /health  — JSON liveness probe (used by systemd watchdog scripts)
    GET /ready   — JSON readiness probe (k8s-style; 503 when bot not ready)
    GET /metrics — Prometheus text exposition format

Usage:
    health = HealthServer(port=8080, bot_ref=bot)
    await health.start()
    ...
    await health.stop()
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from aiohttp import web

from src.infra.logger import get_logger
from src.infra.metrics import render as render_metrics

if TYPE_CHECKING:
    pass

log = get_logger(__name__)

# Timestamp when this process started
_PROCESS_START_TS = time.time()


class HealthServer:
    """Lightweight aiohttp health/metrics server.

    Args:
        port: TCP port to listen on (default 8080)
        bot_ref: optional reference to the Bot instance for status fields.
                 Passed as-is; the server reads attributes defensively.
    """

    def __init__(self, port: int = 8080, bot_ref: Any = None) -> None:
        self.port = port
        self.bot_ref = bot_ref
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._ready: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Build aiohttp app + start TCP listener on 127.0.0.1:{port}."""
        self._app = web.Application()
        self._app.router.add_get("/health", self._handle_health)
        self._app.router.add_get("/ready", self._handle_ready)
        self._app.router.add_get("/metrics", self._handle_metrics)

        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host="127.0.0.1", port=self.port)
        await self._site.start()
        self._ready = True
        log.info("health_server_started", port=self.port)

    async def stop(self) -> None:
        """Gracefully shut down the HTTP server."""
        self._ready = False
        if self._runner:
            try:
                await self._runner.cleanup()
            except Exception as e:
                log.warning("health_server_stop_error", error=str(e))
        log.info("health_server_stopped")

    def mark_ready(self) -> None:
        """Call after Bot.setup() completes to flip /ready → 200."""
        self._ready = True

    def mark_not_ready(self) -> None:
        """Call before shutdown to flip /ready → 503."""
        self._ready = False

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request: web.Request) -> web.Response:
        """GET /health — JSON liveness probe."""
        bot = self.bot_ref
        payload: dict[str, Any] = {
            "status": "ok",
            "uptime_seconds": round(time.time() - _PROCESS_START_TS, 1),
            "bot_running": bot is not None,
        }

        # Defensive attribute reads — bot may still be initialising
        if bot is not None:
            shutdown_set = getattr(getattr(bot, "_shutdown_event", None), "is_set", lambda: None)
            payload["shutdown_requested"] = bool(shutdown_set())

            cb = getattr(bot, "cb", None)
            if cb is not None:
                state = getattr(cb, "state", None)
                if state is not None:
                    payload["cb_paused"] = bool(getattr(state, "is_paused", False))
                    payload["cb_balance_sol"] = getattr(state, "current_balance_sol", None)

            pm = getattr(bot, "position_manager", None)
            if pm is not None:
                payload["open_positions"] = getattr(pm, "open_count", None)

        return web.json_response(payload, status=200)

    async def _handle_metrics(self, request: web.Request) -> web.Response:
        """GET /metrics — Prometheus exposition format."""
        try:
            data = render_metrics()
        except Exception as e:
            log.error("metrics_render_error", error=str(e))
            return web.Response(text=f"# ERROR: {e}\n", status=500)
        return web.Response(
            body=data,
            content_type="text/plain; version=0.0.4",
            charset="utf-8",
        )

    async def _handle_ready(self, request: web.Request) -> web.Response:
        """GET /ready — k8s-style readiness probe."""
        if self._ready:
            return web.json_response({"ready": True}, status=200)
        return web.json_response({"ready": False, "reason": "initialising"}, status=503)
