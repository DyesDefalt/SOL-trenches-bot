"""
Tests for MemeQualityScorer and MemeScoreCache — Phase 10.6.

Covers:
  - Successful single score with mocked LLM response
  - Cost cap exceeded → returns None
  - Invalid JSON from LLM → returns None (via LLMClient's own handling)
  - Schema validation failure → returns None
  - Privacy filter sanitization applied to prompt
  - Cache hit avoids LLM call
  - Cache miss calls LLM and populates cache
  - Batch scoring uses 1 LLM call for multiple tokens
  - is_clone detection present in response
  - feature flag ai_meme_quality_enabled=False → returns None immediately
  - Batch respects feature flag
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.ai.meme_score_cache import MemeScoreCache
from src.ai.meme_quality_scorer import MemeQualityScorer, _build_socials_summary
from src.ai.schemas import MemeQualityScore


# ---------------------------------------------------------------------------
# Fixtures & factories
# ---------------------------------------------------------------------------


def _good_score(**overrides) -> MemeQualityScore:
    defaults = dict(
        overall_score=72,
        visual_clarity=8,
        memetic_potential=8,
        cultural_fit=7,
        originality=6,
        community_signal=5,
        is_clone=False,
        cultural_reference="slowcook dog meme",
        risks=["small holder count"],
        reasoning="Strong visual identity with a recognisable dog character.",
    )
    defaults.update(overrides)
    return MemeQualityScore(**defaults)


def _clone_score() -> MemeQualityScore:
    return MemeQualityScore(
        overall_score=28,
        visual_clarity=3,
        memetic_potential=4,
        cultural_fit=5,
        originality=1,
        community_signal=2,
        is_clone=True,
        cultural_reference="PEPE clone",
        risks=["obvious PEPE copy", "no twitter", "generic frog meme #5000"],
        reasoning="This is a near-identical clone of PEPE with zero differentiation.",
    )


def _mock_llm(return_value: MemeQualityScore | None) -> MagicMock:
    llm = MagicMock()
    llm.complete_structured = AsyncMock(return_value=return_value)
    return llm


def _token_data(**overrides) -> dict:
    base = dict(
        address="So11111111111111111111111111111111111111112",
        name="WifHat",
        symbol="WIF",
        description="A dog wearing a hat on Solana",
        socials={"twitter": "https://twitter.com/wif", "telegram": "https://t.me/wif"},
        narrative_match="dog memes trending",
        created_at="2024-01-10T08:00:00Z",
        mcap_usd=45_000,
        holder_count=820,
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# MemeQualityScorer — single score
# ---------------------------------------------------------------------------


class TestMemeQualityScorerScore:
    @pytest.mark.asyncio
    async def test_successful_score_returns_model(self):
        """Happy path: LLM returns valid data, scorer returns MemeQualityScore."""
        llm = _mock_llm(_good_score())
        scorer = MemeQualityScorer(llm)

        with patch("src.ai.meme_quality_scorer.settings") as mock_cfg:
            mock_cfg.ai_meme_quality_enabled = True
            mock_cfg.llm_fast_model = "google/gemini-2.0-flash"
            mock_cfg.llm_timeout_seconds = 10.0

            result = await scorer.score(_token_data())

        assert result is not None
        assert result.overall_score == 72
        assert result.memetic_potential == 8
        assert result.is_clone is False
        llm.complete_structured.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_feature_flag_disabled_returns_none(self):
        """ai_meme_quality_enabled=False → short-circuit, no LLM call."""
        llm = _mock_llm(_good_score())
        scorer = MemeQualityScorer(llm)

        with patch("src.ai.meme_quality_scorer.settings") as mock_cfg:
            mock_cfg.ai_meme_quality_enabled = False

            result = await scorer.score(_token_data())

        assert result is None
        llm.complete_structured.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cost_cap_exceeded_returns_none(self):
        """LLMClient returns None (cost cap) → scorer propagates None."""
        llm = _mock_llm(None)  # LLMClient already returns None when cap hit
        scorer = MemeQualityScorer(llm)

        with patch("src.ai.meme_quality_scorer.settings") as mock_cfg:
            mock_cfg.ai_meme_quality_enabled = True
            mock_cfg.llm_fast_model = "google/gemini-2.0-flash"
            mock_cfg.llm_timeout_seconds = 10.0

            result = await scorer.score(_token_data())

        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_json_from_llm_returns_none(self):
        """LLMClient returns None on bad JSON (already handled inside client) → None."""
        llm = _mock_llm(None)
        scorer = MemeQualityScorer(llm)

        with patch("src.ai.meme_quality_scorer.settings") as mock_cfg:
            mock_cfg.ai_meme_quality_enabled = True
            mock_cfg.llm_fast_model = "google/gemini-2.0-flash"
            mock_cfg.llm_timeout_seconds = 10.0

            result = await scorer.score(_token_data())

        assert result is None

    @pytest.mark.asyncio
    async def test_schema_validation_failure_returns_none(self):
        """LLMClient returns None when schema validation fails → scorer returns None."""
        llm = _mock_llm(None)
        scorer = MemeQualityScorer(llm)

        with patch("src.ai.meme_quality_scorer.settings") as mock_cfg:
            mock_cfg.ai_meme_quality_enabled = True
            mock_cfg.llm_fast_model = "google/gemini-2.0-flash"
            mock_cfg.llm_timeout_seconds = 10.0

            result = await scorer.score(_token_data())

        assert result is None

    @pytest.mark.asyncio
    async def test_is_clone_detected_in_response(self):
        """Clone detection flag surfaced correctly."""
        llm = _mock_llm(_clone_score())
        scorer = MemeQualityScorer(llm)

        with patch("src.ai.meme_quality_scorer.settings") as mock_cfg:
            mock_cfg.ai_meme_quality_enabled = True
            mock_cfg.llm_fast_model = "google/gemini-2.0-flash"
            mock_cfg.llm_timeout_seconds = 10.0

            result = await scorer.score(_token_data(name="PepeClone", symbol="PEPEC"))

        assert result is not None
        assert result.is_clone is True
        assert result.overall_score == 28
        assert any("PEPE" in r for r in result.risks)

    @pytest.mark.asyncio
    async def test_privacy_filter_applied_to_prompt(self):
        """Sensitive keys in token_data socials URL should not reach LLM as raw secret."""
        llm = _mock_llm(_good_score())
        scorer = MemeQualityScorer(llm)

        sensitive_td = _token_data(
            description="token api_key=sk-supersecret description",
        )

        with patch("src.ai.meme_quality_scorer.settings") as mock_cfg:
            mock_cfg.ai_meme_quality_enabled = True
            mock_cfg.llm_fast_model = "google/gemini-2.0-flash"
            mock_cfg.llm_timeout_seconds = 10.0

            await scorer.score(sensitive_td)

        # Extract the user prompt that was passed to complete_structured
        call_kwargs = llm.complete_structured.call_args
        user_prompt_sent = call_kwargs.kwargs.get("user") or call_kwargs.args[2]
        # PrivacyFilter.sanitize_text should have replaced api_key=... with [REDACTED]
        assert "sk-supersecret" not in user_prompt_sent
        assert "[REDACTED]" in user_prompt_sent

    @pytest.mark.asyncio
    async def test_model_override_passed_to_llm(self):
        """Explicit model kwarg is forwarded to LLM client."""
        llm = _mock_llm(_good_score())
        scorer = MemeQualityScorer(llm, model="anthropic/claude-haiku-4.5")

        with patch("src.ai.meme_quality_scorer.settings") as mock_cfg:
            mock_cfg.ai_meme_quality_enabled = True
            mock_cfg.llm_timeout_seconds = 10.0

            await scorer.score(_token_data())

        call_kwargs = llm.complete_structured.call_args
        model_used = call_kwargs.kwargs.get("model") or call_kwargs.args[0]
        assert model_used == "anthropic/claude-haiku-4.5"


# ---------------------------------------------------------------------------
# MemeQualityScorer — batch scoring
# ---------------------------------------------------------------------------


class TestMemeQualityScorerBatch:
    @pytest.mark.asyncio
    async def test_batch_uses_single_llm_call(self):
        """score_batch with 3 tokens should call LLM exactly once."""

        # Batch result: _BatchMemeScoreRaw with .scores keyed by symbol
        batch_raw = MagicMock()
        batch_raw.scores = {
            "WIF": _good_score().model_dump(),
            "BONK": _good_score(overall_score=55, symbol="BONK").model_dump(),
            "MYRO": _good_score(overall_score=61, symbol="MYRO").model_dump(),
        }

        llm = MagicMock()
        llm.complete_structured = AsyncMock(return_value=batch_raw)

        tokens = [
            _token_data(symbol="WIF", address="addr1"),
            _token_data(symbol="BONK", address="addr2"),
            _token_data(symbol="MYRO", address="addr3"),
        ]

        scorer = MemeQualityScorer(llm)

        with patch("src.ai.meme_quality_scorer.settings") as mock_cfg:
            mock_cfg.ai_meme_quality_enabled = True
            mock_cfg.llm_fast_model = "google/gemini-2.0-flash"
            mock_cfg.llm_timeout_seconds = 10.0

            results = await scorer.score_batch(tokens)

        # One LLM call for the whole batch
        assert llm.complete_structured.await_count == 1
        # Results keyed by address
        assert "addr1" in results
        assert "addr2" in results
        assert "addr3" in results

    @pytest.mark.asyncio
    async def test_batch_feature_flag_disabled(self):
        """ai_meme_quality_enabled=False → all results None, no LLM call."""
        llm = _mock_llm(None)
        scorer = MemeQualityScorer(llm)

        tokens = [_token_data(symbol="AAA", address="aaa"), _token_data(symbol="BBB", address="bbb")]

        with patch("src.ai.meme_quality_scorer.settings") as mock_cfg:
            mock_cfg.ai_meme_quality_enabled = False

            results = await scorer.score_batch(tokens)

        assert all(v is None for v in results.values())
        llm.complete_structured.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_batch_llm_failure_returns_all_none(self):
        """LLM failure for batch → all results stay None."""
        llm = _mock_llm(None)
        scorer = MemeQualityScorer(llm)

        tokens = [_token_data(symbol="AAA", address="aaa")]

        with patch("src.ai.meme_quality_scorer.settings") as mock_cfg:
            mock_cfg.ai_meme_quality_enabled = True
            mock_cfg.llm_fast_model = "google/gemini-2.0-flash"
            mock_cfg.llm_timeout_seconds = 10.0

            results = await scorer.score_batch(tokens)

        assert results.get("aaa") is None


# ---------------------------------------------------------------------------
# MemeScoreCache
# ---------------------------------------------------------------------------


class TestMemeScoreCache:
    @pytest.mark.asyncio
    async def test_cache_miss_returns_none(self):
        """Cold cache → get returns None."""
        cache = MemeScoreCache(cache=None, ttl=300)
        result = await cache.get("So1anaAddr1111")
        assert result is None

    @pytest.mark.asyncio
    async def test_cache_set_then_get_returns_score(self):
        """set then get (in-memory) returns the stored score."""
        cache = MemeScoreCache(cache=None, ttl=300)
        score = _good_score()
        await cache.set("addr123", score)
        hit = await cache.get("addr123")
        assert hit is not None
        assert hit.overall_score == score.overall_score

    @pytest.mark.asyncio
    async def test_cache_hit_avoids_llm_call(self):
        """Caller should skip LLM when cache returns a score."""
        score_cache = MemeScoreCache(cache=None, ttl=300)
        token_addr = "So1anaAddr9999"
        await score_cache.set(token_addr, _good_score())

        llm = _mock_llm(_good_score())
        scorer = MemeQualityScorer(llm)

        # Simulate caller pattern: check cache first, call LLM only on miss
        hit = await score_cache.get(token_addr)
        if hit is None:
            with patch("src.ai.meme_quality_scorer.settings") as mock_cfg:
                mock_cfg.ai_meme_quality_enabled = True
                mock_cfg.llm_fast_model = "google/gemini-2.0-flash"
                mock_cfg.llm_timeout_seconds = 10.0
                hit = await scorer.score(_token_data(address=token_addr))

        assert hit is not None
        llm.complete_structured.assert_not_awaited()  # Cache hit; LLM not called

    @pytest.mark.asyncio
    async def test_cache_miss_calls_llm(self):
        """Cache miss → LLM is called and result cached."""
        score_cache = MemeScoreCache(cache=None, ttl=300)
        token_addr = "So1anaAddrNew"

        llm = _mock_llm(_good_score())
        scorer = MemeQualityScorer(llm)

        hit = await score_cache.get(token_addr)
        assert hit is None  # cold cache

        with patch("src.ai.meme_quality_scorer.settings") as mock_cfg:
            mock_cfg.ai_meme_quality_enabled = True
            mock_cfg.llm_fast_model = "google/gemini-2.0-flash"
            mock_cfg.llm_timeout_seconds = 10.0

            result = await scorer.score(_token_data(address=token_addr))

        assert result is not None
        llm.complete_structured.assert_awaited_once()

        # Now store in cache
        await score_cache.set(token_addr, result)
        second_hit = await score_cache.get(token_addr)
        assert second_hit is not None
        assert second_hit.overall_score == result.overall_score

    @pytest.mark.asyncio
    async def test_cache_expiry_in_memory(self):
        """Entry with TTL=0 is treated as expired on next get."""
        score_cache = MemeScoreCache(cache=None, ttl=0)
        score = _good_score()
        await score_cache.set("expiring_addr", score)

        # Manually expire by rewinding the stored expiry time
        key = "meme_score:expiring_addr"
        if key in score_cache._mem:
            score_dict, _ = score_cache._mem[key]
            score_cache._mem[key] = (score_dict, time.monotonic() - 1)

        hit = await score_cache.get("expiring_addr")
        assert hit is None

    @pytest.mark.asyncio
    async def test_cache_redis_fallback(self):
        """Redis hit populates in-memory layer and returns score."""
        mock_redis = MagicMock()
        score = _good_score()
        # Redis.get returns the dict (as if orjson deserialized it)
        mock_redis.get = AsyncMock(return_value=score.model_dump())
        mock_redis.set = AsyncMock()

        score_cache = MemeScoreCache(cache=mock_redis, ttl=300)
        result = await score_cache.get("redis_addr")

        assert result is not None
        assert result.overall_score == score.overall_score
        # Should also populate in-memory so next call skips Redis
        assert score_cache.mem_size() == 1


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def test_build_socials_summary_dict():
    """Dict socials formatted correctly."""
    summary = _build_socials_summary({"twitter": "https://t.co/wif", "telegram": ""})
    assert "twitter" in summary
    assert "telegram" not in summary  # empty value excluded


def test_build_socials_summary_empty():
    assert _build_socials_summary(None) == "none"
    assert _build_socials_summary({}) == "none"
    assert _build_socials_summary([]) == "none"


def test_build_socials_summary_list():
    summary = _build_socials_summary(["https://twitter.com/foo"])
    assert "twitter.com" in summary
