"""
Dynamic Parameter Tuner (Phase 6c).

TunerAgent menganalisis performa 7 hari terakhir, lalu memberikan ONE rekomendasi
parameter adjustment yang data-driven. Rekomendasi TIDAK auto-apply — user apply
manual via Telegram /applyTuning.

Flow:
1. Query DB: closed positions, win rate per score range, exit reasons, daily PnL
2. Ambil 5 lesson terbaru dari lesson_store
3. Build context + current params + sanitize via PrivacyFilter
4. Call LLM dengan claude-sonnet-4.6 (premium — weekly frequency, cost OK)
5. Simpan ke data/tuning_history.json (FIFO 50 entries)
6. Return TunerRecommendation untuk dikirim ke Telegram

Dijadwalkan: 0 3 * * 1 (Senin 03:00 UTC) via cron.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.ai.privacy_filter import PrivacyFilter
from src.ai.schemas import TunerRecommendation
from src.config import settings
from src.infra.logger import get_logger

if TYPE_CHECKING:
    from src.ai.llm_client import LLMClient
    from src.infra.db import Database

log = get_logger(__name__)

# Model premium untuk weekly strategic review — low frequency makes cost OK
_DEFAULT_MODEL = "anthropic/claude-sonnet-4.6"

# Path untuk tuning history persistence
_TUNING_HISTORY_PATH = Path("data/tuning_history.json")
_TUNING_HISTORY_MAX = 50

_SYSTEM_PROMPT = """\
Review last 7 days bot performance. Suggest ONE small parameter adjustment.

Constraints:
- Suggest small changes (max +/- 20% from current value)
- Provide concrete data justification
- Indicate expected impact
- Flag any concerning patterns

Parameters that can be tuned:
- min_score_to_buy (current default 75, range 70-85)
- hard_sl_pct (current default -45, range -30 to -60)
- tp1_gain_pct (current default 80, range 50-150)
- max_position_size_sol (current default 0.05, range 0.01-0.10)

Output JSON matching TunerRecommendation schema."""


class TunerAgent:
    """
    Weekly performance analyzer + parameter tuner.

    Usage:
        agent = TunerAgent(llm=llm_client, db=database, lesson_store=lesson_store)
        recommendation = await agent.analyze_weekly_performance()
        if recommendation:
            # Send to Telegram, save to history
    """

    def __init__(
        self,
        llm: "LLMClient",
        db: "Database",
        lesson_store: Any,
    ) -> None:
        self._llm = llm
        self._db = db
        self._lesson_store = lesson_store

    async def analyze_weekly_performance(self) -> TunerRecommendation | None:
        """
        Pull last 7 days performance dari DB, ask LLM untuk ONE parameter adjustment.

        Returns:
            TunerRecommendation jika data cukup dan LLM available.
            None jika data insufisien atau LLM unavailable.
        """
        # Step 1: Pull DB data
        try:
            positions = await self._db.get_recent_closed_positions(limit=200)
        except Exception as e:
            log.error("tuner_db_positions_failed", error=str(e))
            positions = []

        try:
            daily_pnl = await self._db.get_daily_pnl(days=7)
        except Exception as e:
            log.error("tuner_db_daily_pnl_failed", error=str(e))
            daily_pnl = []

        # Filter ke 7 hari terakhir
        cutoff_ts = datetime.now(timezone.utc).timestamp() - 7 * 86400
        week_positions = []
        for p in positions:
            exit_ts = p.get("exit_timestamp")
            if exit_ts is None:
                continue
            # Bisa berupa datetime object atau string
            if isinstance(exit_ts, datetime):
                ts = exit_ts.timestamp()
            else:
                try:
                    ts = datetime.fromisoformat(str(exit_ts)).timestamp()
                except (ValueError, TypeError):
                    continue
            if ts >= cutoff_ts:
                week_positions.append(p)

        if len(week_positions) < 3:
            log.info(
                "tuner_insufficient_data",
                week_position_count=len(week_positions),
                min_required=3,
            )
            return None

        # Step 2: Get recent lessons
        lessons: list[str] = []
        try:
            if hasattr(self._lesson_store, "get_recent"):
                raw_lessons = await self._lesson_store.get_recent(limit=5)
                lessons = [
                    str(l.get("lesson", l)) if isinstance(l, dict) else str(l)
                    for l in (raw_lessons or [])
                ]
            elif hasattr(self._lesson_store, "get_top"):
                raw_lessons = self._lesson_store.get_top(5)
                lessons = [
                    str(l.get("lesson", l)) if isinstance(l, dict) else str(l)
                    for l in (raw_lessons or [])
                ]
        except Exception as e:
            log.warning("tuner_lessons_fetch_failed", error=str(e))

        # Step 3: Build analysis context
        context = self._build_performance_context(week_positions, daily_pnl, lessons)

        # Sanitize sebelum kirim ke LLM (hapus wallet addresses etc)
        sanitized = PrivacyFilter.sanitize_context(context)

        # Step 4: Format user prompt
        user_prompt = self._format_user_prompt(sanitized)

        # Step 5: LLM call
        log.info(
            "tuner_llm_call",
            model=_DEFAULT_MODEL,
            week_positions=len(week_positions),
            lessons=len(lessons),
        )
        result = await self._llm.complete_structured(
            model=_DEFAULT_MODEL,
            system=_SYSTEM_PROMPT,
            user=user_prompt,
            response_model=TunerRecommendation,
            max_tokens=1024,
        )

        if result is None:
            log.warning("tuner_llm_returned_none")
            return None

        log.info(
            "tuner_recommendation",
            param=result.parameter,
            current=result.current_value,
            recommended=result.suggested_value,
            confidence=result.confidence,
        )
        return result

    def _build_performance_context(
        self,
        positions: list[dict[str, Any]],
        daily_pnl: list[dict[str, Any]],
        lessons: list[str],
    ) -> dict[str, Any]:
        """Build structured performance context untuk LLM."""
        # Win rate breakdown per score range
        score_buckets: dict[str, dict[str, int]] = {
            "75-79": {"wins": 0, "total": 0},
            "80-84": {"wins": 0, "total": 0},
            "85+": {"wins": 0, "total": 0},
        }

        # Exit reason distribution
        exit_reasons: dict[str, int] = defaultdict(int)

        total_pnl = 0.0
        wins = 0

        for p in positions:
            score = float(p.get("entry_score", 0))
            pnl = float(p.get("realized_pnl_sol", 0))
            exit_reason = str(p.get("exit_reason", "UNKNOWN"))
            won = pnl > 0

            if won:
                wins += 1
            total_pnl += pnl
            exit_reasons[exit_reason] += 1

            # Score bucket
            if 75 <= score < 80:
                bucket = "75-79"
            elif 80 <= score < 85:
                bucket = "80-84"
            elif score >= 85:
                bucket = "85+"
            else:
                continue  # score < 75 tidak sesuai normal operation

            score_buckets[bucket]["total"] += 1
            if won:
                score_buckets[bucket]["wins"] += 1

        # Win rate per bucket
        winrate_by_score = {}
        for bucket, data in score_buckets.items():
            total = data["total"]
            if total > 0:
                winrate_by_score[bucket] = {
                    "trades": total,
                    "wins": data["wins"],
                    "winrate": round(data["wins"] / total, 3),
                }

        # Daily PnL summary
        daily_summary = []
        for d in daily_pnl:
            daily_summary.append({
                "date": str(d.get("date", "")),
                "pnl_sol": round(float(d.get("pnl_sol", 0)), 4),
                "trades": int(d.get("trades_total", 0)),
                "wins": int(d.get("trades_won", 0)),
            })

        overall_winrate = wins / len(positions) if positions else 0.0

        return {
            "period": "last_7_days",
            "overall": {
                "total_trades": len(positions),
                "wins": wins,
                "losses": len(positions) - wins,
                "winrate": round(overall_winrate, 3),
                "total_pnl_sol": round(total_pnl, 4),
            },
            "winrate_by_score_range": winrate_by_score,
            "exit_reason_distribution": dict(exit_reasons),
            "daily_pnl_summary": daily_summary,
            "current_settings": {
                "min_score_to_buy": settings.min_score_to_buy,
                "hard_sl_pct": settings.hard_sl_pct,
                "tp1_gain_pct": settings.tp1_gain_pct,
                "tp2_gain_pct": settings.tp2_gain_pct,
                "tp3_gain_pct": settings.tp3_gain_pct,
                "max_position_size_sol": settings.max_position_size_sol,
                "trailing_stop_pct": settings.trailing_stop_pct,
            },
            "recent_lessons": lessons,
        }

    def _format_user_prompt(self, context: dict[str, Any]) -> str:
        """Format sanitized context sebagai readable LLM user prompt."""
        overall = context.get("overall", {})
        settings_ctx = context.get("current_settings", {})
        winrate_by_score = context.get("winrate_by_score_range", {})
        exit_dist = context.get("exit_reason_distribution", {})
        daily = context.get("daily_pnl_summary", [])
        lessons = context.get("recent_lessons", [])

        lines = [
            "=== WEEKLY PERFORMANCE REPORT ===",
            "",
            f"Period: {context.get('period', 'last_7_days')}",
            f"Total trades: {overall.get('total_trades', 0)}",
            f"Win rate: {float(overall.get('winrate', 0)):.1%}",
            f"Total PnL: {overall.get('total_pnl_sol', 0):.4f} SOL",
            "",
            "--- Win Rate by Score Range ---",
        ]

        for range_label, data in winrate_by_score.items():
            lines.append(
                f"  Score {range_label}: {data.get('trades', 0)} trades, "
                f"{float(data.get('winrate', 0)):.1%} WR"
            )

        lines.append("")
        lines.append("--- Exit Reason Distribution ---")
        for reason, count in exit_dist.items():
            lines.append(f"  {reason}: {count}")

        lines.append("")
        lines.append("--- Daily PnL (7 days) ---")
        for d in daily:
            lines.append(
                f"  {d.get('date', '?')}: {float(d.get('pnl_sol', 0)):+.4f} SOL "
                f"({d.get('trades', 0)}t {d.get('wins', 0)}W)"
            )

        lines.append("")
        lines.append("--- Current Settings ---")
        for k, v in settings_ctx.items():
            lines.append(f"  {k}: {v}")

        if lessons:
            lines.append("")
            lines.append("--- Recent Lessons ---")
            for i, lesson in enumerate(lessons, 1):
                lines.append(f"  {i}. {lesson[:200]}")

        lines.append("")
        lines.append("Based on this data, suggest ONE parameter adjustment with data justification.")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # History persistence
    # ------------------------------------------------------------------
    @staticmethod
    def save_to_history(recommendation: TunerRecommendation) -> None:
        """
        Persist recommendation ke data/tuning_history.json (FIFO 50 entries).

        Thread-safe via file overwrite (single writer).
        """
        _TUNING_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)

        history: list[dict[str, Any]] = []
        if _TUNING_HISTORY_PATH.exists():
            try:
                history = json.loads(_TUNING_HISTORY_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                history = []

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **recommendation.model_dump(),
        }
        history.append(entry)

        # FIFO — keep last 50
        if len(history) > _TUNING_HISTORY_MAX:
            history = history[-_TUNING_HISTORY_MAX:]

        _TUNING_HISTORY_PATH.write_text(json.dumps(history, indent=2))
        log.info(
            "tuner_history_saved",
            path=str(_TUNING_HISTORY_PATH),
            total_entries=len(history),
        )

    @staticmethod
    def load_history() -> list[dict[str, Any]]:
        """Load existing tuning history dari disk."""
        if not _TUNING_HISTORY_PATH.exists():
            return []
        try:
            return json.loads(_TUNING_HISTORY_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return []
