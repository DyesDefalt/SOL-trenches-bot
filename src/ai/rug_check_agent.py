"""
Rug check agent — pre-trade LLM analysis.

Dipanggil sebelum trade execution untuk final veto pada edge cases
yang missed oleh static scoring rules.

Default model: google/gemini-2.0-flash (cheap, fast, good for high-volume).
"""

from __future__ import annotations

from src.ai.llm_client import LLMClient
from src.ai.schemas import RugCheckResult
from src.config import settings
from src.infra.logger import get_logger

log = get_logger(__name__)

_SYSTEM_PROMPT = """You are a Solana memecoin rug detection specialist. Analyze token data and recommend APPROVE / VETO / REDUCE_SIZE.

Look for red flags:
- Copycat naming patterns of recent rugs
- Suspiciously rapid mcap growth
- Bundle pattern in holder distribution
- Dev wallet alts cluster
- Liquidity provider is freshly created wallet
- Top10 holders > 60% concentration

You are an ADVISOR. The static rules pipeline already passed this token. Your job is final veto for edge cases the formula missed.

Output JSON matching this schema strictly:
{
  "veto": bool,
  "confidence": float (0.0-1.0),
  "reason": string,
  "red_flags": list[string],
  "recommendation": "APPROVE" | "VETO" | "REDUCE_SIZE"
}"""


class RugCheckAgent:
    """
    Pre-trade rug check via LLM.

    Returns RugCheckResult atau None jika LLM tidak tersedia.
    Caller wajib fall back ke static rules kalau None.
    """

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    async def assess(
        self,
        token_address: str,
        symbol: str,
        mcap_usd: float,
        liquidity_usd: float,
        age_minutes: int,
        holder_count: int,
        top10_pct: float,
        dev_holding_pct: float,
        bundle_supply_pct: float,
        lp_burned: bool,
        is_renounced: bool,
        gmgn_security_score: int,
        smart_money_count: int,
        smart_money_buyers: list[str],
        recent_lessons: list[str] | None = None,
    ) -> RugCheckResult | None:
        """
        Pre-trade rug check. Returns None jika LLM unavailable.

        Caller falls back ke static rules kalau return None.
        """
        if not settings.ai_enabled or not settings.ai_rug_check_enabled:
            return None

        # Truncate smart_money_buyers to first 5 (privacy + prompt size)
        buyers_preview = smart_money_buyers[:5]

        # Build user prompt
        lessons_text = ""
        if recent_lessons:
            lessons_text = "\n\nRecent lessons from closed trades:\n" + "\n".join(
                f"- {lesson}" for lesson in recent_lessons
            )

        user_prompt = f"""Token to analyze:
- Address: {token_address}
- Symbol: {symbol}
- Market Cap: ${mcap_usd:,.0f} USD
- Liquidity: ${liquidity_usd:,.0f} USD
- Age: {age_minutes} minutes
- Holder Count: {holder_count}
- Top 10 Holders: {top10_pct:.1f}%
- Dev Holding: {dev_holding_pct:.1f}%
- Bundle Supply: {bundle_supply_pct:.1f}%
- LP Burned: {lp_burned}
- Contract Renounced: {is_renounced}
- GMGN Security Score: {gmgn_security_score}/100
- Smart Money Buyers Count: {smart_money_count}
- Smart Money Buyer Addresses (first 5): {buyers_preview}{lessons_text}

Analyze this token and return your rug check assessment as JSON."""

        result = await self._llm.complete_structured(
            model=settings.llm_fast_model,
            system=_SYSTEM_PROMPT,
            user=user_prompt,
            response_model=RugCheckResult,
            max_tokens=400,
            timeout=settings.llm_timeout_seconds,
        )

        if result is not None:
            log.info(
                "rug_check_complete",
                token=symbol,
                recommendation=result.recommendation,
                confidence=result.confidence,
                veto=result.veto,
                red_flags_count=len(result.red_flags),
            )

        return result
