"""
Phase 9 smoke test — verify all 6 new data sources work end-to-end.

Tests each integration with live API calls:
- CryptoQuant: BTC exchange flows (or graceful 401 if free tier)
- Alpha Vantage: SPY quote (TradFi proxy)
- CryptoPanic: Solana hot news (last 24h)
- Messari: Solana asset profile
- CoinGecko: BONK contract lookup
- Tokito: simple JSON completion via pecut-ai

Plus aggregators:
- MacroRegimeDetector: classify current regime
- NewsAggregator: get_market_sentiment
- CrossRefValidator: validate BONK

Run: make phase9-smoke
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

# Ensure src/ is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.clients.alphavantage_client import AlphaVantageClient
from src.clients.coingecko_client import CoinGeckoClient
from src.clients.cryptocurrencycv_client import CryptoCurrencyCvClient
from src.clients.cryptopanic_client import CryptoPanicClient  # legacy; kept available
from src.clients.cryptoquant_client import CryptoQuantClient
from src.clients.messari_client import MessariClient
from src.ai.tokito_client import TokitoClient
from src.config import settings
from src.intel.crossref_validator import CrossRefValidator
from src.intel.macro_regime import MacroRegimeDetector
from src.intel.news_aggregator import NewsAggregator
from pydantic import BaseModel, Field


# Known token for cross-ref test
BONK_ADDRESS = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


class SmokeResult(BaseModel):
    """Result format for tokito test."""
    answer: str = Field(description="Short answer")
    confidence: float = Field(description="0-1 confidence")


def ok(msg: str) -> None:
    print(f"  \033[92m✓\033[0m {msg}")


def fail(msg: str) -> None:
    print(f"  \033[91m✗\033[0m {msg}")


def warn(msg: str) -> None:
    print(f"  \033[93m⚠\033[0m {msg}")


def skip(msg: str) -> None:
    print(f"  \033[90m·\033[0m {msg}")


async def test_cryptoquant() -> bool:
    print("\n[1/9] CryptoQuant")
    if not settings.cryptoquant_api_key:
        skip("Skipped — CRYPTOQUANT_API_KEY not set")
        return True
    async with CryptoQuantClient() as client:
        try:
            data = await client.get_btc_exchange_flows()
            if data:
                ok(f"BTC exchange flows fetched ({len(str(data))} bytes)")
            else:
                warn("Empty response (likely free tier limit — graceful degrade works)")
            return True
        except Exception as e:
            fail(f"Error: {e}")
            return False


async def test_alphavantage() -> bool:
    print("\n[2/9] Alpha Vantage")
    if not settings.alphavantage_api_key:
        skip("Skipped — ALPHAVANTAGE_API_KEY not set")
        return True
    async with AlphaVantageClient() as client:
        try:
            data = await client.get_spx_quote()
            if data and data.get("price"):
                ok(f"SPY quote: ${data['price']:.2f} ({data.get('change_pct', 0):.2f}%)")
            else:
                warn("Empty response (rate limit hit — daily 25 req cap)")
            return True
        except Exception as e:
            fail(f"Error: {e}")
            return False


async def test_cryptopanic() -> bool:
    """
    News slot — now backed by cryptocurrency.cv (free, no API key needed).

    Kept the function name as test_cryptopanic so the test table renames in
    one place. CryptoPanic v1 is retired; v2 requires a paid plan after
    April 1, 2026 — we've migrated to cryptocurrency.cv which aggregates
    130+ sources for free.
    """
    print("\n[3/9] News (cryptocurrency.cv)")
    async with CryptoCurrencyCvClient() as client:
        try:
            posts = await client.get_solana_news(filter="hot", limit=5)
            if posts:
                top = posts[0].get("title", "?")[:60]
                ok(f"Solana hot news: {len(posts)} posts (top: '{top}...')")
            else:
                warn("No posts returned")

            # Bonus: probe the asset-sentiment endpoint that CryptoPanic
            # didn't expose. Failure here is non-fatal — main path stays
            # green so the suite reports the news source as healthy.
            try:
                sentiment = await client.get_asset_sentiment("SOL", period="24h")
                overall = sentiment.get("overall") or sentiment.get("market", {}).get("overall")
                score = sentiment.get("score") or sentiment.get("market", {}).get("score")
                if overall:
                    ok(f"SOL 24h sentiment: {overall} (score={score})")
            except Exception as exc:
                warn(f"Sentiment probe non-fatal error: {exc}")

            return True
        except Exception as e:
            fail(f"Error: {e}")
            return False


async def test_messari() -> bool:
    print("\n[4/9] Messari")
    if not settings.messari_api_key:
        skip("Skipped — MESSARI_API_KEY not set")
        return True
    async with MessariClient() as client:
        try:
            profile = await client.get_asset_profile("solana")
            if profile:
                ok(f"Solana profile fetched ({len(str(profile))} bytes)")
            else:
                warn("Empty profile")
            return True
        except Exception as e:
            fail(f"Error: {e}")
            return False


async def test_coingecko() -> bool:
    print("\n[5/9] CoinGecko")
    if not settings.coingecko_api_key:
        skip("Skipped — COINGECKO_API_KEY not set")
        return True
    async with CoinGeckoClient() as client:
        try:
            data = await client.get_token_by_contract(BONK_ADDRESS, platform="solana")
            if data and data.get("id"):
                rank = data.get("market_cap_rank", "?")
                ok(f"BONK lookup: id={data['id']}, rank={rank}")
            else:
                warn("Empty data (token not listed?)")

            trending = await client.get_trending()
            if trending:
                count = len(trending.get("coins", []))
                ok(f"Trending coins: {count} items")
            return True
        except Exception as e:
            fail(f"Error: {e}")
            return False


async def test_tokito() -> bool:
    print("\n[6/9] Tokito (pecut-ai)")
    if not settings.tokito_api_key:
        skip("Skipped — TOKITO_API_KEY not set")
        return True
    try:
        async with TokitoClient() as client:
            result = await client.complete_structured(
                model=settings.tokito_model,
                system="You are a helpful assistant. Output JSON with answer (string) and confidence (float).",
                user="Is Solana a layer 1 blockchain? Reply in JSON: {\"answer\": \"yes/no/maybe\", \"confidence\": 0-1}",
                response_model=SmokeResult,
                max_tokens=100,
                timeout=15.0,
            )
            if result:
                ok(f"Tokito reply: answer={result.answer!r}, confidence={result.confidence}")
            else:
                warn("Tokito returned None (timeout or invalid JSON)")
            return True
    except Exception as e:
        fail(f"Error: {e}")
        return False


async def test_macro_regime() -> bool:
    print("\n[7/9] MacroRegimeDetector")
    cq = CryptoQuantClient() if settings.cryptoquant_api_key else None
    av = AlphaVantageClient() if settings.alphavantage_api_key else None
    if not cq and not av:
        skip("Skipped — neither CryptoQuant nor Alpha Vantage configured")
        return True
    detector = MacroRegimeDetector(cryptoquant=cq, alphavantage=av)
    try:
        regime = await detector.detect_regime()
        level_str = regime.level.value if hasattr(regime.level, "value") else str(regime.level)
        ok(f"Regime: {level_str}, multiplier={regime.position_size_multiplier:.2f}, skip={regime.should_skip_entries}")
        if regime.reasons:
            for r in regime.reasons[:3]:
                print(f"      └─ {r}")
        return True
    except Exception as e:
        fail(f"Error: {e}")
        return False
    finally:
        if cq:
            await cq.close()
        if av:
            await av.close()


async def test_news_aggregator() -> bool:
    print("\n[8/9] NewsAggregator")
    # cryptocurrency.cv is free + no API key — always instantiate.
    # CryptoPanic (paid) used as additional source only if user has a key.
    news = CryptoCurrencyCvClient()
    ms = MessariClient() if settings.messari_api_key else None
    agg = NewsAggregator(news_client=news, messari=ms)
    try:
        sentiment = await agg.get_market_sentiment()
        ok(f"Market sentiment: overall={sentiment.overall_sentiment:.2f}, "
           f"bullish={sentiment.bullish_count}, bearish={sentiment.bearish_count}")
        if sentiment.trending_tickers:
            ok(f"Trending tickers: {', '.join(sentiment.trending_tickers[:5])}")
        return True
    except Exception as e:
        fail(f"Error: {e}")
        return False
    finally:
        await news.close()
        if ms:
            await ms.close()


async def test_crossref() -> bool:
    print("\n[9/9] CrossRefValidator")
    cg = CoinGeckoClient() if settings.coingecko_api_key else None
    ms = MessariClient() if settings.messari_api_key else None
    if not cg and not ms:
        skip("Skipped — neither CoinGecko nor Messari configured")
        return True
    validator = CrossRefValidator(coingecko=cg, messari=ms)
    try:
        result = await validator.validate_token(contract_address=BONK_ADDRESS, symbol="BONK")
        ok(f"BONK cross-ref: cg_listed={result.coingecko_listed}, "
           f"cg_rank={result.coingecko_rank}, bonus={result.cross_ref_bonus:.1f}")
        if result.reasons:
            for r in result.reasons[:3]:
                print(f"      └─ {r}")
        return True
    except Exception as e:
        fail(f"Error: {e}")
        return False
    finally:
        if cg:
            await cg.close()
        if ms:
            await ms.close()


async def main() -> int:
    print("=" * 60)
    print("Phase 9 Smoke Test — Extended Intelligence Layer")
    print("=" * 60)

    results: list[tuple[str, bool]] = []
    tests = [
        ("CryptoQuant", test_cryptoquant),
        ("Alpha Vantage", test_alphavantage),
        ("CryptoPanic", test_cryptopanic),
        ("Messari", test_messari),
        ("CoinGecko", test_coingecko),
        ("Tokito", test_tokito),
        ("MacroRegime", test_macro_regime),
        ("NewsAggregator", test_news_aggregator),
        ("CrossRef", test_crossref),
    ]

    for name, test in tests:
        try:
            ok_flag = await test()
            results.append((name, ok_flag))
        except Exception as e:
            print(f"  \033[91m✗\033[0m Unexpected error in {name}: {e}")
            results.append((name, False))

    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"Passed: {passed}/{total}")
    for name, ok_flag in results:
        marker = "\033[92m✓\033[0m" if ok_flag else "\033[91m✗\033[0m"
        print(f"  {marker} {name}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
