"""
Smart Wallet Quality Assessment via LLM (Phase 6b).

WalletAssessor dipanggil saat mengklasifikasi wallet baru di SmartWalletRegistry.
LLM menganalisis trading style dari stats + recent trades untuk klasifikasi yang
lebih nuanced daripada pure winrate/profit threshold.

Flow:
1. Fetch wallet stats 30d dari GMGN (winrate, profit, trade count)
2. Fetch recent 20 trades dari GMGN
3. Kalau data insufisien (< 5 trades) → return None
4. Build context + sanitize via PrivacyFilter
5. Call LLM dengan claude-haiku-4.5 (nuanced + cost-efficient)
6. Cache result 24h di Redis

Caller (SmartWalletRegistry) fall back ke standard classification kalau return None.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.ai.privacy_filter import PrivacyFilter
from src.ai.schemas import WalletAssessment
from src.infra.cache import cached
from src.infra.logger import get_logger

if TYPE_CHECKING:
    from src.ai.llm_client import LLMClient

log = get_logger(__name__)

# Module-level singleton filter (stateless, fine to share)
_privacy = PrivacyFilter()

# Model default: haiku untuk latency + cost efficiency, tapi nuanced reasoning
_DEFAULT_MODEL = "anthropic/claude-haiku-4.5"

# Minimal trade count untuk assessment yang reliable
_MIN_TRADE_COUNT = 5

_SYSTEM_PROMPT = """\
Analyze this Solana memecoin trader wallet for trading style and quality.

Classify as:
- ALPHA_TRADER: consistent profits, swing 30min-4h hold, diversified
- WASH_TRADER: suspicious split-modal pattern across alts (multiple wallets same trades)
- SCALPER: hold <5min, won't match our 30min cycle
- POSITION_TRADER: hold >1day, signals come too late for us
- REGIME_DEPENDENT: only profits in bull market
- UNCLEAR: not enough data

Our bot needs 30-min swing memecoin trader pattern. Recommend tier:
- A: high-conviction alpha trader matching our style
- B: moderate alpha trader
- C: backup tier (low priority)
- BLACKLIST: wash trader or scammer
- F: skip (style mismatch but not malicious)

Output JSON matching WalletAssessment schema."""


class WalletAssessor:
    """
    LLM-based smart wallet quality assessor.

    Instantiate sekali, pakai untuk classify banyak wallets.
    """

    def __init__(self, llm: "LLMClient", gmgn_client: Any) -> None:
        self._llm = llm
        self._gmgn = gmgn_client

    @cached(prefix="ai:wallet_assessment", ttl=86400)  # 24h
    async def assess(
        self,
        wallet_address: str,
        chain: str = "sol",
    ) -> WalletAssessment | None:
        """
        Fetch wallet data dari GMGN, ask LLM to classify trading style.

        Returns:
            WalletAssessment jika data cukup dan LLM available.
            None jika data insufisien atau LLM unavailable — caller falls back ke
            standard tier classification.
        """
        # Step 1: Fetch 30d stats
        try:
            stats = await self._gmgn.get_wallet_stats(wallet_address, chain=chain, period="30d")
        except Exception as e:
            log.warning("assessor_stats_fetch_failed", wallet=wallet_address[:8], error=str(e))
            return None

        if not stats:
            log.debug("assessor_empty_stats", wallet=wallet_address[:8])
            return None

        # Step 2: Fetch recent trades
        try:
            activity = await self._gmgn.get_wallet_activity(wallet_address, chain=chain, limit=20)
        except Exception as e:
            log.warning("assessor_activity_fetch_failed", wallet=wallet_address[:8], error=str(e))
            activity = []

        # Step 3: Check insufficient data gate
        trade_count = len(activity)
        if trade_count < _MIN_TRADE_COUNT:
            log.debug(
                "assessor_insufficient_trades",
                wallet=wallet_address[:8],
                trade_count=trade_count,
                min_required=_MIN_TRADE_COUNT,
            )
            return None

        # Step 4: Build context
        context = self._build_context(wallet_address, stats, activity)

        # Sanitize — remove raw addresses before sending to LLM
        sanitized = PrivacyFilter.sanitize_context(context)

        # Step 5: Build user prompt dari sanitized context
        user_prompt = self._format_user_prompt(sanitized)

        # Step 6: LLM call
        log.info("assessor_llm_call", wallet=wallet_address[:8], model=_DEFAULT_MODEL)
        result = await self._llm.complete_structured(
            model=_DEFAULT_MODEL,
            system=_SYSTEM_PROMPT,
            user=user_prompt,
            response_model=WalletAssessment,
        )

        if result is None:
            log.warning("assessor_llm_returned_none", wallet=wallet_address[:8])
            return None

        log.info(
            "assessor_complete",
            wallet=wallet_address[:8],
            style=result.classification,
            tier=result.recommended_tier,
            confidence=result.confidence,
        )
        return result

    def _build_context(
        self,
        wallet_address: str,
        stats: dict[str, Any],
        activity: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Build structured context untuk LLM prompt."""
        winrate = float(stats.get("winrate", 0))
        realized_profit = float(stats.get("realized_profit", 0))
        total_profit = float(stats.get("total_profit", 0))
        buy_count = int(stats.get("buy_count", 0))
        sell_count = int(stats.get("sell_count", 0))
        token_num = int(stats.get("token_num", 0))

        # Summarize recent trades dalam format ringkas
        trade_summaries = []
        for trade in activity[:20]:
            side = trade.get("side", "?")
            token = trade.get("token_symbol", trade.get("base_symbol", "?"))
            amount = float(trade.get("amount_usd", trade.get("amount", 0)))
            timestamp = trade.get("timestamp", trade.get("block_time", 0))
            # Hold time kalau tersedia dari trade data
            hold_minutes = trade.get("hold_minutes", None)
            hold_str = f"{hold_minutes:.0f}min" if hold_minutes else "?"
            trade_summaries.append(
                f"{side},{token},{amount:.0f}USD,hold={hold_str},ts={timestamp}"
            )

        return {
            "wallet": wallet_address,
            "stats_30d": {
                "winrate": winrate,
                "realized_profit_sol": realized_profit,
                "total_profit_sol": total_profit,
                "buy_count": buy_count,
                "sell_count": sell_count,
                "unique_tokens_traded": token_num,
            },
            "recent_trades_csv": "\n".join(trade_summaries),
            "trade_count_sampled": len(activity),
        }

    def _format_user_prompt(self, context: dict[str, Any]) -> str:
        """Format sanitized context sebagai readable LLM user prompt."""
        stats = context.get("stats_30d", {})
        return (
            f"Wallet: {context.get('wallet', 'REDACTED')}\n\n"
            f"30-Day Stats:\n"
            f"  Win rate: {float(stats.get('winrate', 0)):.1%}\n"
            f"  Realized profit: {stats.get('realized_profit_sol', 0):.2f} SOL\n"
            f"  Total profit: {stats.get('total_profit_sol', 0):.2f} SOL\n"
            f"  Buy count: {stats.get('buy_count', 0)}\n"
            f"  Sell count: {stats.get('sell_count', 0)}\n"
            f"  Unique tokens traded: {stats.get('unique_tokens_traded', 0)}\n\n"
            f"Recent Trades (side,token,amount,hold_time,timestamp):\n"
            f"{context.get('recent_trades_csv', 'none')}\n\n"
            f"Based on this data, classify the wallet's trading style and recommend tier."
        )
