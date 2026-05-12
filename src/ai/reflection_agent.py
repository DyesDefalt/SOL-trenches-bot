"""
Reflection agent — post-trade analysis.

Dipanggil fire-and-forget setelah position close. Analyze trade,
extract lesson, save ke LessonStore untuk dipakai oleh RugCheckAgent
sebagai context di trade berikutnya.

Default model: anthropic/claude-haiku-4.5 (better nuanced reasoning vs flash).
"""

from __future__ import annotations

from src.ai.lesson_store import LessonStore
from src.ai.llm_client import LLMClient
from src.ai.schemas import TradeReflection
from src.config import settings
from src.infra.logger import get_logger

log = get_logger(__name__)

_SYSTEM_PROMPT = """You are analyzing a completed memecoin trade. Extract lessons about what worked, what didn't.

Classifications:
- PROFITABLE_AS_EXPECTED: profit AND signals predicted it correctly
- LUCKY_PROFIT: profit BUT signals were weak/wrong (lucky)
- UNPROFITABLE_AS_EXPECTED: loss AND signals predicted weakness
- UNEXPECTED_LOSS: loss DESPITE good signals (most concerning)

For UNEXPECTED_LOSS: spend extra effort identifying what was misleading.

Output JSON matching this schema:
{
  "trade_classification": "PROFITABLE_AS_EXPECTED" | "LUCKY_PROFIT" | "UNPROFITABLE_AS_EXPECTED" | "UNEXPECTED_LOSS",
  "key_signals": list[string],
  "misleading_signals": list[string],
  "emerging_pattern": string | null,
  "recommended_adjustment": dict | null,
  "lesson_summary": string (1 sentence, concise)
}"""


class ReflectionAgent:
    """
    Post-trade reflection agent.

    Analyze closed trade, extract lesson, save ke LessonStore.
    Fire-and-forget — tidak block main trading loop.
    """

    def __init__(self, llm: LLMClient, store: LessonStore) -> None:
        self._llm = llm
        self._store = store

    async def reflect_on_trade(
        self,
        position_id: int,
        token_symbol: str,
        token_address: str,
        entry_score: float,
        entry_price_usd: float,
        exit_price_usd: float,
        entry_amount_sol: float,
        smart_money_count: int,
        pnl_sol: float,
        pnl_pct: float,
        holding_minutes: float,
        exit_reason: str,
        recent_trades_summary: str = "",
    ) -> None:
        """
        Analyze closed trade, save lesson. Fire-and-forget.

        Tidak raise exception — error dilog dan di-skip agar tidak
        mengganggu main trading loop.
        """
        if not settings.ai_enabled or not settings.ai_reflection_enabled:
            return

        direction = "profit" if pnl_sol >= 0 else "loss"

        user_prompt = f"""Completed trade to analyze:
- Position ID: {position_id}
- Token: {token_symbol} ({token_address})
- Entry Score: {entry_score:.1f}/100
- Entry Price: ${entry_price_usd:.8f}
- Exit Price: ${exit_price_usd:.8f}
- Entry Amount: {entry_amount_sol:.4f} SOL
- Smart Money Count at Entry: {smart_money_count}
- PnL: {pnl_sol:+.4f} SOL ({pnl_pct:+.1f}%) — {direction}
- Holding Time: {holding_minutes:.1f} minutes
- Exit Reason: {exit_reason}"""

        if recent_trades_summary:
            user_prompt += f"\n\nRecent trades context:\n{recent_trades_summary}"

        user_prompt += "\n\nClassify this trade and extract a lesson."

        try:
            result: TradeReflection | None = await self._llm.complete_structured(
                model=settings.llm_reasoning_model,
                system=_SYSTEM_PROMPT,
                user=user_prompt,
                response_model=TradeReflection,
                max_tokens=500,
                timeout=settings.llm_timeout_seconds,
            )
        except Exception as e:
            log.error(
                "reflection_llm_error",
                position_id=position_id,
                token=token_symbol,
                error=str(e),
            )
            return

        if result is None:
            log.warning(
                "reflection_no_result",
                position_id=position_id,
                token=token_symbol,
            )
            return

        # Save lesson ke store
        lesson_record = {
            "position_id": position_id,
            "token_symbol": token_symbol,
            "token_address": token_address,
            "pnl_sol": pnl_sol,
            "pnl_pct": pnl_pct,
            "trade_classification": result.trade_classification,
            "key_signals": result.key_signals,
            "misleading_signals": result.misleading_signals,
            "emerging_pattern": result.emerging_pattern,
            "recommended_adjustment": result.recommended_adjustment,
            "lesson_summary": result.lesson_summary,
            "exit_reason": exit_reason,
            "holding_minutes": holding_minutes,
        }

        try:
            await self._store.add_lesson(lesson_record)
            log.info(
                "trade_reflection_saved",
                position_id=position_id,
                token=token_symbol,
                classification=result.trade_classification,
                lesson=result.lesson_summary[:100],
            )
        except Exception as e:
            log.error(
                "reflection_store_error",
                position_id=position_id,
                error=str(e),
            )
