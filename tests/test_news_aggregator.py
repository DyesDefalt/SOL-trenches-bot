"""
Tests for NewsAggregator.

Coverage:
- get_market_sentiment: with CryptoPanic data, with no client (graceful)
- check_token_narrative: narrative match, FUD detection, Messari profile check
- detect_fud_events: FUD keyword matching, severity, ordering
- _compute_narrative_bonus: all scoring rules
- Both clients None → graceful empty results
"""

from __future__ import annotations

import sys
import types


# ---------------------------------------------------------------------------
# Stub out infra modules (Python 3.9 compat)
# ---------------------------------------------------------------------------

def _stub_infra_modules():
    if "src.infra.cache" not in sys.modules:
        m = types.ModuleType("src.infra.cache")
        def cached(prefix: str, ttl: int, skip_cache=None):
            def decorator(func):
                return func
            return decorator
        m.cached = cached
        sys.modules["src.infra.cache"] = m

    if "src.infra.rate_limiter" not in sys.modules:
        m = types.ModuleType("src.infra.rate_limiter")
        class TokenBucket:
            def __init__(self, rps=1.0, burst=None, name=""):
                pass
            async def acquire(self):
                pass
        m.TokenBucket = TokenBucket
        sys.modules["src.infra.rate_limiter"] = m

    if "src.infra.logger" not in sys.modules:
        m = types.ModuleType("src.infra.logger")

        class _KwLogger:
            """Structlog-compatible logger stub that accepts keyword args."""
            def __init__(self, name: str):
                self.name = name
            def _noop(self, msg, *args, **kwargs): pass
            debug = info = warning = error = critical = _noop

        m.get_logger = lambda name: _KwLogger(name)
        sys.modules["src.infra.logger"] = m


_stub_infra_modules()

# ---------------------------------------------------------------------------
# Now safe to import
# ---------------------------------------------------------------------------

from unittest.mock import AsyncMock  # noqa: E402

import pytest  # noqa: E402

from src.intel.news_aggregator import (  # noqa: E402
    FUDEvent,
    MarketSentiment,
    NewsAggregator,
    TokenNarrative,
    _compute_narrative_bonus,
    _title_has_fud,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_post(
    title: str = "SOL pumps",
    sentiment_score: float = 0.5,
    currencies: list | None = None,
    positive: int = 5,
    negative: int = 1,
    important: int = 2,
    url: str = "https://example.com",
    published_at: str = "2026-05-12T10:00:00Z",
) -> dict:
    return {
        "title": title,
        "url": url,
        "source": "CoinDesk",
        "published_at": published_at,
        "currencies": currencies or ["SOL"],
        "votes": {"positive": positive, "negative": negative, "important": important},
        "sentiment_score": sentiment_score,
    }


def _mock_cp(
    sol_news: list | None = None,
    token_news: list | None = None,
    trending: list | None = None,
) -> AsyncMock:
    cp = AsyncMock()
    cp.get_solana_news = AsyncMock(return_value=sol_news or [])
    cp.get_token_news = AsyncMock(return_value=token_news or [])
    cp.get_trending_currencies = AsyncMock(return_value=trending or [])
    return cp


def _mock_messari(profile: dict | None = None) -> AsyncMock:
    m = AsyncMock()
    m.get_asset_profile = AsyncMock(return_value=profile or {})
    m.find_slug_by_contract = AsyncMock(return_value=None)
    return m


# ---------------------------------------------------------------------------
# Unit: _title_has_fud
# ---------------------------------------------------------------------------

class TestTitleHasFud:
    def test_hack_detected(self):
        assert _title_has_fud("Protocol hack drains $10M") is True

    def test_rug_detected(self):
        assert _title_has_fud("BONK dev rugs community") is True

    def test_sec_case_insensitive(self):
        assert _title_has_fud("SEC files lawsuit against exchange") is True

    def test_clean_title_not_fud(self):
        assert _title_has_fud("SOL reaches new ATH") is False

    def test_empty_string_not_fud(self):
        assert _title_has_fud("") is False


# ---------------------------------------------------------------------------
# Unit: _compute_narrative_bonus
# ---------------------------------------------------------------------------

class TestComputeNarrativeBonus:
    def test_trending_ticker_adds_5(self):
        bonus = _compute_narrative_bonus("BONK", 0.0, 0, ["SOL", "BONK", "WIF"], False)
        assert bonus == 5.0

    def test_positive_sentiment_adds_3(self):
        bonus = _compute_narrative_bonus("WIF", 0.5, 0, [], False)
        assert bonus == 3.0

    def test_high_mention_count_adds_2(self):
        bonus = _compute_narrative_bonus("WIF", 0.0, 10, [], False)
        assert bonus == 2.0

    def test_negative_sentiment_subtracts_5(self):
        bonus = _compute_narrative_bonus("WIF", -0.5, 0, [], False)
        assert bonus == -5.0

    def test_fud_event_returns_minus_10(self):
        # Even with trending + positive sentiment, FUD overrides to -10
        bonus = _compute_narrative_bonus("SCAM", 0.9, 100, ["SCAM", "SOL"], has_fud=True)
        assert bonus == -10.0

    def test_all_bonuses_stacked_capped_at_10(self):
        # Trending +5, positive +3, high mentions +2 = 10 (at cap)
        bonus = _compute_narrative_bonus("BONK", 0.5, 10, ["BONK"], False)
        assert bonus == pytest.approx(10.0)

    def test_bonus_floored_at_minus_10(self):
        bonus = _compute_narrative_bonus("X", -0.9, 0, [], True)
        assert bonus >= -10.0


# ---------------------------------------------------------------------------
# Integration: get_market_sentiment
# ---------------------------------------------------------------------------

class TestGetMarketSentiment:
    @pytest.mark.asyncio
    async def test_no_cryptopanic_returns_neutral_sentiment(self):
        agg = NewsAggregator(cryptopanic=None, messari=None)
        sentiment = await agg.get_market_sentiment()
        assert isinstance(sentiment, MarketSentiment)
        assert sentiment.overall_sentiment == 0.0
        assert sentiment.trending_tickers == []

    @pytest.mark.asyncio
    async def test_with_posts_computes_positive_sentiment(self):
        bullish = [_make_post(sentiment_score=0.8) for _ in range(3)]
        bearish = [_make_post(sentiment_score=-0.6)]
        cp = _mock_cp()
        cp.get_solana_news = AsyncMock(side_effect=[bullish, bullish, bearish])
        cp.get_trending_currencies = AsyncMock(return_value=["SOL", "BONK"])
        agg = NewsAggregator(cryptopanic=cp, messari=None)
        sentiment = await agg.get_market_sentiment()
        assert sentiment.bullish_count == 3
        assert sentiment.bearish_count == 1
        assert sentiment.overall_sentiment > 0

    @pytest.mark.asyncio
    async def test_top_headlines_limited_to_5(self):
        posts = [_make_post(important=i) for i in range(10)]
        cp = _mock_cp()
        cp.get_solana_news = AsyncMock(return_value=posts)
        cp.get_trending_currencies = AsyncMock(return_value=[])
        agg = NewsAggregator(cryptopanic=cp, messari=None)
        sentiment = await agg.get_market_sentiment()
        assert len(sentiment.top_headlines) <= 5


# ---------------------------------------------------------------------------
# Integration: check_token_narrative
# ---------------------------------------------------------------------------

class TestCheckTokenNarrative:
    @pytest.mark.asyncio
    async def test_narrative_match_when_ticker_trending(self):
        cp = _mock_cp(
            token_news=[_make_post(sentiment_score=0.5)],
            trending=["BONK", "WIF", "SOL"],
        )
        agg = NewsAggregator(cryptopanic=cp, messari=None)
        narrative = await agg.check_token_narrative("BONK")
        assert isinstance(narrative, TokenNarrative)
        assert narrative.narrative_match is True
        assert narrative.symbol == "BONK"

    @pytest.mark.asyncio
    async def test_no_narrative_match_when_not_trending(self):
        cp = _mock_cp(
            token_news=[_make_post(sentiment_score=0.1)],
            trending=["SOL", "WIF"],
        )
        agg = NewsAggregator(cryptopanic=cp, messari=None)
        narrative = await agg.check_token_narrative("BONK")
        assert narrative.narrative_match is False

    @pytest.mark.asyncio
    async def test_fud_keyword_gives_minus_10_bonus(self):
        cp = _mock_cp(
            token_news=[_make_post(title="BONK developers exploit contract", sentiment_score=-0.8)],
            trending=["BONK"],
        )
        agg = NewsAggregator(cryptopanic=cp, messari=None)
        narrative = await agg.check_token_narrative("BONK")
        assert narrative.narrative_bonus == -10.0

    @pytest.mark.asyncio
    async def test_messari_profile_found_sets_is_listed_true(self):
        cp = _mock_cp(token_news=[], trending=[])
        messari = _mock_messari(profile={"slug": "bonk", "name": "Bonk"})
        agg = NewsAggregator(cryptopanic=cp, messari=messari)
        narrative = await agg.check_token_narrative("BONK")
        assert narrative.is_listed_on_messari is True

    @pytest.mark.asyncio
    async def test_messari_not_found_sets_is_listed_false(self):
        cp = _mock_cp(token_news=[], trending=[])
        messari = _mock_messari(profile={})
        agg = NewsAggregator(cryptopanic=cp, messari=messari)
        narrative = await agg.check_token_narrative("NEWTOKEN")
        assert narrative.is_listed_on_messari is False

    @pytest.mark.asyncio
    async def test_both_clients_none_returns_safe_defaults(self):
        agg = NewsAggregator(cryptopanic=None, messari=None)
        narrative = await agg.check_token_narrative("BONK")
        assert isinstance(narrative, TokenNarrative)
        assert narrative.symbol == "BONK"
        assert narrative.mention_count == 0
        assert narrative.narrative_bonus == 0.0


# ---------------------------------------------------------------------------
# Integration: detect_fud_events
# ---------------------------------------------------------------------------

class TestDetectFudEvents:
    @pytest.mark.asyncio
    async def test_detects_fud_keyword_in_news(self):
        cp = _mock_cp()
        cp.get_token_news = AsyncMock(return_value=[
            _make_post(title="BONK smart contract exploit discovered", important=6, negative=12),
        ])
        agg = NewsAggregator(cryptopanic=cp, messari=None)
        events = await agg.detect_fud_events(["BONK"])
        assert len(events) == 1
        assert events[0].symbol == "BONK"
        assert "exploit" in events[0].title.lower()

    @pytest.mark.asyncio
    async def test_no_fud_keywords_returns_empty_list(self):
        cp = _mock_cp()
        cp.get_token_news = AsyncMock(return_value=[
            _make_post(title="BONK reaches new high"),
        ])
        agg = NewsAggregator(cryptopanic=cp, messari=None)
        events = await agg.detect_fud_events(["BONK"])
        assert events == []

    @pytest.mark.asyncio
    async def test_high_severity_sorted_first(self):
        cp = _mock_cp()
        cp.get_token_news = AsyncMock(side_effect=[
            [_make_post(title="WIF rug pull confirmed", important=1, negative=1)],   # low
            [_make_post(title="SOL SEC lawsuit filed", important=7, negative=15)],   # high
        ])
        agg = NewsAggregator(cryptopanic=cp, messari=None)
        events = await agg.detect_fud_events(["WIF", "SOL"])
        assert events[0].severity == "high"
        assert events[-1].severity == "low"

    @pytest.mark.asyncio
    async def test_no_cryptopanic_returns_empty(self):
        agg = NewsAggregator(cryptopanic=None, messari=None)
        events = await agg.detect_fud_events(["BONK", "WIF"])
        assert events == []

    @pytest.mark.asyncio
    async def test_empty_symbols_returns_empty(self):
        cp = _mock_cp()
        agg = NewsAggregator(cryptopanic=cp, messari=None)
        events = await agg.detect_fud_events([])
        assert events == []
