"""
Structured logging via structlog. JSON output di production, pretty di dev.

Usage:
    from src.infra.logger import get_logger
    log = get_logger(__name__)
    log.info("token_scored", token=token_address, score=85, smart_money_count=3)
"""

from __future__ import annotations

import logging
import sys

import structlog

from src.config import settings


def configure_logging() -> None:
    """Setup structlog. Panggil sekali saat startup."""
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        timestamper,
    ]

    if settings.env == "development":
        # Pretty-print untuk human reading
        renderer = structlog.dev.ConsoleRenderer(colors=True)
    else:
        # JSON untuk log aggregator / Grafana / Loki
        shared_processors.append(structlog.processors.format_exc_info)
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Get a logger bound dengan module name."""
    return structlog.get_logger(name)


# Auto-configure saat first import
configure_logging()
