"""
Weekly tuner — analyze last 7 days, send recommendation to Telegram.

Cron: 0 3 * * 1 (Monday 03:00 UTC)

Usage:
    python scripts/run_weekly_tuner.py

Exit codes:
    0 — success (recommendation sent or insufficient data)
    1 — fatal error (DB/LLM init failed)
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure project root on sys.path saat dijalankan langsung
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.ai.llm_client import LLMClient
from src.ai.tuner_agent import TunerAgent
from src.config import settings
from src.infra.db import Database
from src.infra.logger import get_logger

log = get_logger(__name__)


class _SimpleLessonStore:
    """
    Minimal lesson store stub kalau lesson_store belum ada.

    Reads dari data/lessons.json kalau file exists, otherwise returns empty.
    """

    _PATH = Path("data/lessons.json")

    def get_top(self, limit: int = 5) -> list[dict]:
        if not self._PATH.exists():
            return []
        try:
            data = json.loads(self._PATH.read_text())
            return data[-limit:] if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []


def _format_telegram_message(rec) -> str:
    """Format TunerRecommendation sebagai Telegram HTML message."""
    change_pct = (rec.suggested_value - rec.current_value) / abs(rec.current_value) * 100 if rec.current_value != 0 else 0
    change_sign = "+" if change_pct >= 0 else ""
    confidence_bar = "█" * int(rec.confidence * 10) + "░" * (10 - int(rec.confidence * 10))

    warning = ""
    if rec.warning_flags:
        flags = "\n".join(f"  ⚠️ {f}" for f in rec.warning_flags[:3])
        warning = f"\n\n<b>Warning Flags:</b>\n{flags}"

    return (
        f"📊 <b>Weekly Tuner Recommendation</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>Parameter:</b> <code>{rec.parameter}</code>\n"
        f"<b>Current:</b> <code>{rec.current_value}</code>\n"
        f"<b>Suggested:</b> <code>{rec.suggested_value}</code> "
        f"(<code>{change_sign}{change_pct:.1f}%</code>)\n\n"
        f"<b>Confidence:</b> {confidence_bar} {rec.confidence:.0%}\n\n"
        f"<b>Justification:</b>\n{rec.justification}\n\n"
        f"<b>Expected Impact:</b>\n{rec.expected_impact}"
        f"{warning}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>To apply: /applyTuning {rec.parameter} {rec.suggested_value}</i>\n"
        f"<i>Note: in-memory only. Edit secrets/.env to persist.</i>"
    )


async def main() -> int:
    """Main entry point. Returns exit code."""
    log.info(
        "weekly_tuner_start",
        timestamp=datetime.now(timezone.utc).isoformat(),
        dry_run=settings.dry_run,
    )

    # --- Init components ---
    db: Database | None = None
    llm: LLMClient | None = None

    try:
        db = Database()
        await db.connect()
        log.info("tuner_db_connected")
    except Exception as e:
        log.error("tuner_db_init_failed", error=str(e))
        return 1

    try:
        llm = LLMClient()
    except Exception as e:
        log.error("tuner_llm_init_failed", error=str(e))
        if db:
            await db.close()
        return 1

    lesson_store = _SimpleLessonStore()

    # --- Run tuner ---
    agent = TunerAgent(llm=llm, db=db, lesson_store=lesson_store)

    try:
        recommendation = await agent.analyze_weekly_performance()
    except Exception as e:
        log.error("tuner_agent_failed", error=str(e))
        await _cleanup(db, llm)
        return 1

    if recommendation is None:
        log.info("tuner_insufficient_data_exit")
        await _cleanup(db, llm)
        return 0

    # --- Save to history ---
    try:
        TunerAgent.save_to_history(recommendation)
        log.info(
            "tuner_history_written",
            param=recommendation.param_name,
            recommended=recommendation.recommended_value,
        )
    except Exception as e:
        log.warning("tuner_history_save_failed", error=str(e))

    # --- Send Telegram alert ---
    message = _format_telegram_message(recommendation)
    await _send_telegram(message)

    log.info(
        "weekly_tuner_done",
        param=recommendation.parameter,
        current=recommendation.current_value,
        recommended=recommendation.suggested_value,
        confidence=recommendation.confidence,
    )

    await _cleanup(db, llm)
    return 0


async def _send_telegram(message: str) -> None:
    """Send message to Telegram via direct HTTP (no bot polling needed)."""
    token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id

    if not token or not chat_id:
        log.warning("tuner_telegram_not_configured")
        return

    import httpx

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                url,
                data={
                    "chat_id": chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                },
            )
            if resp.status_code == 200:
                log.info("tuner_telegram_sent")
            else:
                log.warning(
                    "tuner_telegram_send_failed",
                    status=resp.status_code,
                    body=resp.text[:200],
                )
    except Exception as e:
        log.warning("tuner_telegram_exception", error=str(e))


async def _cleanup(db: Database | None, llm: LLMClient | None) -> None:
    """Cleanup connections."""
    if db:
        try:
            await db.close()
        except Exception:
            pass
    if llm:
        try:
            await llm.close()
        except Exception:
            pass


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
