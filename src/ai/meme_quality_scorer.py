"""
Meme Quality Scorer — Phase 10.6 LLM-based memecoin quality evaluation.

Evaluates Solana memecoins on five human-judgment dimensions:
  1. visual_clarity    — clear, memorable identity
  2. memetic_potential — relatable, shareable, viral-ready
  3. cultural_fit      — matches current crypto/internet zeitgeist
  4. originality       — not a clone of PEPE, WIF, BONK, etc.
  5. community_signal  — real engagement vs bot/empty accounts

Uses OpenRouter (primary) or Tokito (fallback) via injected llm_client.
All failures return None — caller must handle static fallback.

Usage::

    scorer = MemeQualityScorer(llm_client)
    result = await scorer.score(token_data)
    if result is None:
        # static fallback — score stays unchanged
        ...

Batch::

    results = await scorer.score_batch([token1, token2, ...])
    # returns {address: MemeQualityScore | None, ...}
"""

from __future__ import annotations

import json
from typing import Any

from src.ai.privacy_filter import PrivacyFilter
from src.ai.schemas import MemeQualityScore
from src.config import settings
from src.infra.logger import get_logger

log = get_logger(__name__)

# JSON schema snippet embedded in user prompt so LLM knows exact field names/types
_SCHEMA_HINT = """{
  "overall_score": <int 0-100>,
  "visual_clarity": <int 0-10>,
  "memetic_potential": <int 0-10>,
  "cultural_fit": <int 0-10>,
  "originality": <int 0-10>,
  "community_signal": <int 0-10>,
  "is_clone": <bool>,
  "cultural_reference": "<string, empty if none>",
  "risks": ["<risk1>", "<risk2>"],
  "reasoning": "<one-paragraph explanation>"
}"""

_SYSTEM_PROMPT = (
    "You are an expert at evaluating Solana memecoin quality. "
    "You evaluate tokens on 5 dimensions and return strict JSON only. "
    "Never include markdown code fences or extra text outside the JSON object."
)

_USER_PROMPT_TEMPLATE = """\
Evaluate this Solana memecoin for trading quality:

Name: {name}
Symbol: {symbol}
Description: {description}
Created: {created_at}
Market cap: ${mcap_usd}
Holders: {holder_count}
Socials: {socials_summary}
Narrative match in news: {narrative_match}

Score the meme on:
1. visual_clarity (0-10): Does the concept have a clear, memorable identity?
2. memetic_potential (0-10): Is it relatable, shareable, viral-ready?
3. cultural_fit (0-10): Does it match current crypto/internet zeitgeist?
4. originality (0-10): Is it original or a clone of trending tokens like PEPE, WIF, BONK?
5. community_signal (0-10): Real engagement on socials, or empty/bot accounts?

Also flag:
- is_clone: true if obviously copying a trending meme
- cultural_reference: what zeitgeist it taps into (if any)
- risks: specific quality risks (e.g., "no twitter", "generic dog meme #4000")

overall_score should integrate all 5 dimensions weighted toward memetic_potential and originality.

Output strict JSON matching this schema:
{schema}"""

_BATCH_SYSTEM_PROMPT = (
    "You are an expert at evaluating Solana memecoin quality. "
    "You evaluate a batch of tokens and return strict JSON only — "
    "a single JSON object mapping each token's symbol to its score object. "
    "Never include markdown code fences or extra text outside the JSON."
)

_BATCH_USER_PROMPT_TEMPLATE = """\
Evaluate these Solana memecoins for trading quality. Return a single JSON object where each key is the token symbol.

Tokens:
{tokens_block}

For each token, score:
1. visual_clarity (0-10): Clear, memorable identity?
2. memetic_potential (0-10): Relatable, shareable, viral-ready?
3. cultural_fit (0-10): Matches current crypto/internet zeitgeist?
4. originality (0-10): Original or clone of PEPE, WIF, BONK, etc.?
5. community_signal (0-10): Real engagement or empty/bot accounts?

Also flag:
- is_clone: true if obviously copying a trending meme
- cultural_reference: zeitgeist tapped (empty string if none)
- risks: list of specific quality risks
- overall_score: integrated 0-100, weighted toward memetic_potential and originality
- reasoning: one-paragraph explanation

Output format (keyed by symbol):
{{
  "SYMBOL1": {schema},
  "SYMBOL2": {schema}
}}"""


def _build_socials_summary(socials: dict[str, str] | list | None) -> str:
    """Collapse socials dict/list into a short readable string."""
    if not socials:
        return "none"
    if isinstance(socials, dict):
        parts = []
        for platform, url in socials.items():
            if url:
                parts.append(f"{platform}: {url}")
        return ", ".join(parts) if parts else "none"
    if isinstance(socials, list):
        return ", ".join(str(s) for s in socials if s) or "none"
    return str(socials)


def _token_block(td: dict[str, Any]) -> str:
    """Format a single token_data dict as a readable block for batch prompts."""
    return (
        f"Symbol: {td.get('symbol', 'UNKNOWN')}\n"
        f"  Name: {td.get('name', '')}\n"
        f"  Description: {td.get('description', '')}\n"
        f"  Created: {td.get('created_at', 'unknown')}\n"
        f"  Market cap: ${td.get('mcap_usd', 0)}\n"
        f"  Holders: {td.get('holder_count', 0)}\n"
        f"  Socials: {_build_socials_summary(td.get('socials'))}\n"
        f"  Narrative match: {td.get('narrative_match', 'none')}"
    )


class MemeQualityScorer:
    """
    LLM-based memecoin quality scorer.

    Constructor:
        llm_client  — LLMClient or TokitoClient (injected, pass from llm_provider)
        model       — override model; defaults to settings.llm_fast_model
    """

    def __init__(self, llm_client: Any, model: str | None = None) -> None:
        self._llm = llm_client
        self._model = model or settings.llm_fast_model

    async def score(self, token_data: dict[str, Any]) -> MemeQualityScore | None:
        """
        Score a single token. Returns None on cost cap, timeout, or parse failure.

        token_data keys used:
          name, symbol, description, socials, narrative_match,
          created_at, mcap_usd, holder_count
        """
        if not settings.ai_meme_quality_enabled:
            return None

        name = token_data.get("name", "")
        symbol = token_data.get("symbol", "")
        description = token_data.get("description", "") or ""
        created_at = token_data.get("created_at", "unknown")
        mcap_usd = token_data.get("mcap_usd", 0)
        holder_count = token_data.get("holder_count", 0)
        narrative_match = token_data.get("narrative_match", "none") or "none"
        socials_summary = _build_socials_summary(token_data.get("socials"))

        user_prompt = _USER_PROMPT_TEMPLATE.format(
            name=name,
            symbol=symbol,
            description=description[:300],  # cap description length
            created_at=created_at,
            mcap_usd=f"{mcap_usd:,.0f}" if isinstance(mcap_usd, (int, float)) else mcap_usd,
            holder_count=holder_count,
            socials_summary=socials_summary,
            narrative_match=str(narrative_match)[:200],
            schema=_SCHEMA_HINT,
        )

        # Sanitize via PrivacyFilter before sending
        user_prompt_clean = PrivacyFilter.sanitize_text(user_prompt)

        result = await self._llm.complete_structured(
            model=self._model,
            system=_SYSTEM_PROMPT,
            user=user_prompt_clean,
            response_model=MemeQualityScore,
            max_tokens=400,
            timeout=settings.llm_timeout_seconds,
        )

        if result is not None:
            log.info(
                "meme_score_complete",
                symbol=symbol,
                overall=result.overall_score,
                is_clone=result.is_clone,
                memetic=result.memetic_potential,
                originality=result.originality,
            )

        return result

    async def score_batch(
        self, tokens: list[dict[str, Any]]
    ) -> dict[str, MemeQualityScore | None]:
        """
        Score up to 10 tokens in a single LLM call.

        Returns dict keyed by token address (falls back to symbol if no address).
        Tokens beyond 10 are scored individually to avoid prompt size blow-up.
        """
        if not settings.ai_meme_quality_enabled:
            return {
                td.get("address") or td.get("symbol", f"token_{i}"): None
                for i, td in enumerate(tokens)
            }

        # Build address→token_data mapping for result assembly
        keyed: dict[str, dict[str, Any]] = {}
        for i, td in enumerate(tokens):
            key = td.get("address") or td.get("symbol") or f"token_{i}"
            keyed[key] = td

        results: dict[str, MemeQualityScore | None] = {k: None for k in keyed}

        # Split into batches of 10
        batch_keys = list(keyed.keys())
        batch_size = 10

        for batch_start in range(0, len(batch_keys), batch_size):
            chunk_keys = batch_keys[batch_start : batch_start + batch_size]
            chunk = [keyed[k] for k in chunk_keys]

            tokens_block = "\n\n".join(_token_block(td) for td in chunk)
            user_prompt = _BATCH_USER_PROMPT_TEMPLATE.format(
                tokens_block=tokens_block,
                schema=_SCHEMA_HINT,
            )
            user_prompt_clean = PrivacyFilter.sanitize_text(user_prompt)

            # Use a raw complete call because batch result keyed by symbol,
            # not a single Pydantic model — we parse manually.
            raw_result = await self._llm.complete_structured(
                model=self._model,
                system=_BATCH_SYSTEM_PROMPT,
                user=user_prompt_clean,
                response_model=_BatchMemeScoreRaw,
                max_tokens=400 * len(chunk),
                timeout=settings.llm_timeout_seconds,
            )

            if raw_result is None:
                log.warning(
                    "meme_score_batch_llm_fail",
                    batch_start=batch_start,
                    batch_size=len(chunk),
                )
                continue

            # raw_result.scores is dict[str, dict]
            for addr_key, td in zip(chunk_keys, chunk):
                symbol = td.get("symbol", "")
                raw_score = raw_result.scores.get(symbol)
                if raw_score is None:
                    # Try matching by address or positional key
                    raw_score = raw_result.scores.get(addr_key)
                if raw_score is None:
                    log.warning(
                        "meme_score_batch_missing_symbol",
                        symbol=symbol,
                        addr_key=addr_key,
                        available_keys=list(raw_result.scores.keys()),
                    )
                    continue
                try:
                    parsed = MemeQualityScore.model_validate(raw_score)
                    results[addr_key] = parsed
                    log.debug(
                        "meme_score_batch_ok",
                        symbol=symbol,
                        overall=parsed.overall_score,
                    )
                except Exception as e:
                    log.warning(
                        "meme_score_batch_schema_fail",
                        symbol=symbol,
                        error=str(e),
                    )

        return results


class _BatchMemeScoreRaw:
    """
    Internal shim: LLMClient.complete_structured expects a Pydantic model,
    but batch responses are a free-form JSON object keyed by symbol.

    We use a minimal Pydantic model that holds the raw dict, then parse
    each entry into MemeQualityScore individually.
    """

    from pydantic import BaseModel as _BaseModel

    class _Inner(_BaseModel):
        scores: dict[str, Any]

        @classmethod
        def model_validate(cls, data: Any) -> "_BatchMemeScoreRaw._Inner":  # type: ignore[override]
            # data may be the full JSON object already keyed by symbol
            if isinstance(data, dict):
                return cls(scores=data)
            raise ValueError(f"Expected dict for batch scores, got {type(data)}")

    # Expose as top-level for isinstance checks in complete_structured
    scores: dict[str, Any]

    @classmethod
    def model_validate(cls, data: Any) -> "_BatchMemeScoreRaw":  # type: ignore[misc]
        obj = cls()
        if isinstance(data, dict):
            obj.scores = data
        else:
            raise ValueError(f"Expected dict for batch scores, got {type(data)}")
        return obj
