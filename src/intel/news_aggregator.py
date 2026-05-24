"""
News & Narrative Aggregator — unified intelligence from CryptoPanic + Messari.

Detects narrative cycles (Trump coins, AI tokens, animal memes, etc.) and
FUD events (hacks, exploits, rugs, SEC actions) that affect memecoin prices.

Output feeds into scoring engine as narrative_bonus (-10 to +10).

Scoring rules:
- +5 if ticker in trending_tickers
- +3 if sentiment_score > 0.3
- +2 if mention_count > 5 in 24h
- -5 if sentiment_score < -0.3
- -10 if FUD event detected for token

FUD keywords: hack, exploit, drained, scam, rug, ponzi, SEC, lawsuit, delisted
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.infra.logger import get_logger

if TYPE_CHECKING:
    # The aggregator uses duck-typed news clients — anything implementing
    # `get_solana_news`, `get_token_news`, `get_trending_currencies` works.
    # Prefer CryptoCurrencyCvClient (free, no key, replaces CryptoPanic v1).
    from src.clients.cryptocurrencycv_client import CryptoCurrencyCvClient
    from src.clients.cryptopanic_client import CryptoPanicClient
    from src.clients.messari_client import MessariClient

    NewsClient = CryptoCurrencyCvClient | CryptoPanicClient

log = get_logger(__name__)

_FUD_KEYWORDS = frozenset([
    "hack", "exploit", "drained", "scam", "rug", "ponzi",
    "sec", "lawsuit", "delisted",
])


def _title_has_fud(title: str) -> bool:
    """Return True if title contains any FUD keyword (case-insensitive)."""
    lower = title.lower()
    return any(kw in lower for kw in _FUD_KEYWORDS)


@dataclass
class MarketSentiment:
    """Overall Solana market sentiment snapshot."""
    overall_sentiment: float        # -1.0 to 1.0
    bullish_count: int
    bearish_count: int
    trending_tickers: list[str]     # top mentioned in last hour
    top_headlines: list[dict]       # 5 most important


@dataclass
class TokenNarrative:
    """Narrative + sentiment snapshot for a single token."""
    symbol: str
    narrative_match: bool           # True if ticker appears in trending
    sentiment_score: float          # -1.0 to 1.0
    mention_count: int              # in last 24h
    narrative_bonus: float          # -10 to +10 for scoring
    is_listed_on_messari: bool      # token has Messari profile
    news_items: list[dict] = field(default_factory=list)


@dataclass
class FUDEvent:
    """A detected FUD event for a specific token."""
    symbol: str
    severity: str                   # "high", "medium", "low"
    title: str
    url: str
    published_at: str


def _compute_narrative_bonus(
    symbol: str,
    sentiment_score: float,
    mention_count: int,
    trending_tickers: list[str],
    has_fud: bool,
) -> float:
    """
    Compute narrative_bonus in range [-10, +10].

    Rules:
    +5  ticker in trending_tickers
    +3  sentiment_score > 0.3
    +2  mention_count > 5
    -5  sentiment_score < -0.3
    -10 FUD event detected (replaces other negatives — most severe)
    """
    if has_fud:
        return -10.0

    bonus: float = 0.0

    if symbol.upper() in [t.upper() for t in trending_tickers]:
        bonus += 5.0

    if sentiment_score > 0.3:
        bonus += 3.0
    elif sentiment_score < -0.3:
        bonus -= 5.0

    if mention_count > 5:
        bonus += 2.0

    return max(-10.0, min(10.0, bonus))


def _assess_fud_severity(votes: dict) -> str:
    """Assess FUD severity from vote counts."""
    negative = int(votes.get("negative", 0) or 0)
    important = int(votes.get("important", 0) or 0)
    if important >= 5 or negative >= 10:
        return "high"
    if important >= 2 or negative >= 3:
        return "medium"
    return "low"


class NewsAggregator:
    """
    Unified news + narrative intelligence from a news client + Messari.

    The news_client is duck-typed — currently supports either:
    - CryptoCurrencyCvClient (RECOMMENDED, free, no key)
    - CryptoPanicClient (legacy; requires paid plan post-April-2026)

    Both clients can be None (graceful degradation).

    Usage::

        # Preferred — cryptocurrency.cv (free):
        from src.clients.cryptocurrencycv_client import CryptoCurrencyCvClient
        aggregator = NewsAggregator(news_client=CryptoCurrencyCvClient(),
                                    messari=messari_client)

        # Legacy still works:
        aggregator = NewsAggregator(cryptopanic=cryptopanic_client, ...)
    """

    def __init__(
        self,
        news_client: "NewsClient | None" = None,
        messari: "MessariClient | None" = None,
        cryptopanic: "CryptoPanicClient | None" = None,
    ) -> None:
        # `cryptopanic=` kept for backward-compat with existing callers; the
        # canonical parameter is `news_client`. If both are provided,
        # news_client wins.
        self._cp = news_client or cryptopanic
        self._messari = messari

    # ------------------------------------------------------------------
    # Market-wide sentiment
    # ------------------------------------------------------------------

    async def get_market_sentiment(self) -> MarketSentiment:
        """
        Aggregate overall Solana market sentiment from available sources.

        Fetches hot + bullish + bearish posts in parallel for richer signal.
        """
        if self._cp is None:
            log.debug("news_aggregator_no_cryptopanic")
            return MarketSentiment(
                overall_sentiment=0.0,
                bullish_count=0,
                bearish_count=0,
                trending_tickers=[],
                top_headlines=[],
            )

        hot_posts, bullish_posts, bearish_posts, trending = await asyncio.gather(
            self._cp.get_solana_news(filter="hot", limit=20),
            self._cp.get_solana_news(filter="bullish", limit=20),
            self._cp.get_solana_news(filter="bearish", limit=20),
            self._cp.get_trending_currencies(),
            return_exceptions=True,
        )

        # Coerce exceptions to empty
        hot_posts = hot_posts if isinstance(hot_posts, list) else []
        bullish_posts = bullish_posts if isinstance(bullish_posts, list) else []
        bearish_posts = bearish_posts if isinstance(bearish_posts, list) else []
        trending = trending if isinstance(trending, list) else []

        bullish_count = len(bullish_posts)
        bearish_count = len(bearish_posts)

        all_posts = hot_posts + bullish_posts + bearish_posts
        if all_posts:
            scores = [p["sentiment_score"] for p in all_posts]
            overall_sentiment = sum(scores) / len(scores)
        else:
            overall_sentiment = 0.0

        # Top 5 headlines: prefer important votes
        top_5 = sorted(
            hot_posts,
            key=lambda p: p["votes"]["important"],
            reverse=True,
        )[:5]

        return MarketSentiment(
            overall_sentiment=overall_sentiment,
            bullish_count=bullish_count,
            bearish_count=bearish_count,
            trending_tickers=trending[:20],
            top_headlines=top_5,
        )

    # ------------------------------------------------------------------
    # Per-token narrative
    # ------------------------------------------------------------------

    async def check_token_narrative(
        self,
        symbol: str,
        contract_address: str | None = None,
    ) -> TokenNarrative:
        """
        Check narrative health for a specific token.

        Fetches CryptoPanic token news + Messari profile in parallel.
        Computes sentiment_score, mention_count, and narrative_bonus.
        """
        # Fetch in parallel
        async def _empty_list():
            return []

        cp_task = (
            self._cp.get_token_news(symbol, filter="hot")
            if self._cp else _empty_list()
        )
        trending_task = (
            self._cp.get_trending_currencies()
            if self._cp else _empty_list()
        )

        # Messari: try to find slug
        messari_task = self._messari_profile_task(symbol, contract_address)

        token_news, trending_tickers, messari_data = await asyncio.gather(
            cp_task,
            trending_task,
            messari_task,
            return_exceptions=True,
        )

        token_news = token_news if isinstance(token_news, list) else []
        trending_tickers = trending_tickers if isinstance(trending_tickers, list) else []
        messari_data = messari_data if isinstance(messari_data, dict) else {}

        mention_count = len(token_news)

        if token_news:
            scores = [p["sentiment_score"] for p in token_news]
            sentiment_score = sum(scores) / len(scores)
        else:
            sentiment_score = 0.0

        # Check for FUD in token's own news
        has_fud = any(_title_has_fud(p.get("title", "")) for p in token_news)

        narrative_match = bool(
            trending_tickers and
            symbol.upper() in [t.upper() for t in trending_tickers]
        )

        narrative_bonus = _compute_narrative_bonus(
            symbol=symbol,
            sentiment_score=sentiment_score,
            mention_count=mention_count,
            trending_tickers=trending_tickers,
            has_fud=has_fud,
        )

        log.info(
            "token_narrative_computed",
            symbol=symbol,
            sentiment_score=sentiment_score,
            mention_count=mention_count,
            narrative_match=narrative_match,
            narrative_bonus=narrative_bonus,
            has_fud=has_fud,
        )

        return TokenNarrative(
            symbol=symbol,
            narrative_match=narrative_match,
            sentiment_score=sentiment_score,
            mention_count=mention_count,
            narrative_bonus=narrative_bonus,
            is_listed_on_messari=bool(messari_data),
            news_items=token_news[:10],
        )

    async def _messari_profile_task(
        self,
        symbol: str,
        contract_address: str | None,
    ) -> dict:
        """Fetch Messari profile, trying slug lookup by contract first."""
        if self._messari is None:
            return {}
        try:
            # If we have contract address, try reverse lookup first
            if contract_address:
                slug = await self._messari.find_slug_by_contract(contract_address)
                if slug:
                    return await self._messari.get_asset_profile(slug)

            # Fallback: try symbol as slug directly (lowercased)
            return await self._messari.get_asset_profile(symbol.lower())
        except Exception as e:
            log.debug("messari_profile_task_error", symbol=symbol, error=str(e))
            return {}

    # ------------------------------------------------------------------
    # FUD detection
    # ------------------------------------------------------------------

    async def detect_fud_events(self, symbols: list[str]) -> list[FUDEvent]:
        """
        Scan bearish/important news for FUD events affecting given symbols.

        Searches titles for hack/exploit/rug/SEC/lawsuit keywords.
        Returns list of FUDEvent sorted by severity (high first).
        """
        if self._cp is None or not symbols:
            return []

        # Fetch bearish + important news in parallel for each symbol
        tasks = [
            self._cp.get_token_news(symbol, filter="bearish")
            for symbol in symbols
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        fud_events: list[FUDEvent] = []

        for symbol, news_items in zip(symbols, results):
            if isinstance(news_items, Exception):
                log.debug("fud_detection_fetch_error", symbol=symbol, error=str(news_items))
                continue
            if not isinstance(news_items, list):
                continue

            for post in news_items:
                title = post.get("title", "")
                if not _title_has_fud(title):
                    continue

                severity = _assess_fud_severity(post.get("votes", {}))
                fud_events.append(FUDEvent(
                    symbol=symbol,
                    severity=severity,
                    title=title,
                    url=post.get("url", ""),
                    published_at=post.get("published_at", ""),
                ))

        # Sort: high → medium → low
        severity_order = {"high": 0, "medium": 1, "low": 2}
        fud_events.sort(key=lambda e: severity_order.get(e.severity, 3))

        if fud_events:
            log.warning(
                "fud_events_detected",
                count=len(fud_events),
                symbols=[e.symbol for e in fud_events],
            )

        return fud_events
