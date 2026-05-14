"""
Global Fee Analyzer — detect wash trading via fee-to-volume ratio.

Indonesian degen insight (@PradonoNovaldo, @badidoyo, @ELPonyin):
Each Solana AMM swap pays ~0.25% to the LP pool. Fake volume (wash trading)
inflates raw volume numbers BUT does not generate proportionate fee revenue
because wash traders recycle the same capital — the fee mismatch is the tell.

Algorithm:
  1. Fetch pair data from DexScreener (volume.h1, fdv.h1 fees if available)
  2. expected_fee = volume_usd * 0.0025 (standard Raydium/Orca fee)
  3. actual_fee = pair.fees.h1 (if reported) OR estimated from tx count heuristic
  4. ratio = actual_fee / expected_fee
  5. Classify by ratio thresholds

DexScreener's `fees` field is available on many pairs but not all. When absent
we use a conservative UNKNOWN label rather than fabricating a ratio.

Note on fee sources:
  DexScreener does not always expose fees.h1/h24 in the free API response.
  When available, the field lives under pair["fees"]["h1"] or pair["fees"]["h24"].
  When absent: label = UNKNOWN, score_adjustment = 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.infra.logger import get_logger

if TYPE_CHECKING:
    from src.intel.birdeye_client import BirdeyeClient
    from src.intel.dexscreener_client import DexscreenerClient

log = get_logger(__name__)

# Standard Solana AMM LP fee (Raydium v4, Orca, Meteora default)
_STANDARD_LP_FEE = 0.0025  # 0.25%


@dataclass
class FeeAnalysis:
    """
    Result of fee-to-volume analysis for a token.

    label:
      WASH_TRADING — volume is likely fake (fee far below expected)
      SUSPICIOUS   — fee lower than expected, possible partial wash
      ORGANIC      — fee proportional to volume (healthy trading)
      UNUSUAL      — fee above expected (high slippage or premium DEX)
      UNKNOWN      — insufficient data to classify

    fee_volume_ratio: actual_fee / expected_fee (0.0 if UNKNOWN)
    score_adjustment: contribution to composite score (-10 to +10)
    reasoning: human-readable explanation
    """

    label: str  # WASH_TRADING | SUSPICIOUS | ORGANIC | UNUSUAL | UNKNOWN
    fee_volume_ratio: float
    score_adjustment: float  # -10 to +10
    reasoning: str


# Score adjustments per label
_SCORE_MAP: dict[str, float] = {
    "WASH_TRADING": -10.0,
    "SUSPICIOUS": -5.0,
    "ORGANIC": 5.0,
    "UNUSUAL": 0.0,
    "UNKNOWN": 0.0,
}


def _classify_ratio(ratio: float) -> str:
    """Map fee/volume ratio to a label string."""
    if ratio < 0.4:
        return "WASH_TRADING"
    if ratio < 0.7:
        return "SUSPICIOUS"
    if ratio <= 1.3:
        return "ORGANIC"
    return "UNUSUAL"


class GlobalFeeAnalyzer:
    """
    Analyze fee-to-volume ratio to detect wash trading.

    Constructor:
        dexscreener: DexscreenerClient (required) — primary data source.
        birdeye:     BirdeyeClient (optional) — reserved for future enrichment.

    Usage::

        analyzer = GlobalFeeAnalyzer(dexscreener_client)
        result = await analyzer.analyze("TokenMintXxx")
        if result.label == "WASH_TRADING":
            # hard reject
    """

    def __init__(
        self,
        dexscreener: "DexscreenerClient",
        birdeye: "BirdeyeClient | None" = None,
    ) -> None:
        self._dex = dexscreener
        self._birdeye = birdeye  # reserved for future use

    async def analyze(
        self,
        token_address: str,
        hours: int = 1,
    ) -> FeeAnalysis:
        """
        Perform fee analysis for token_address over the given time window.

        hours: 1 or 24 (maps to DexScreener h1/h24 fields).
        Returns FeeAnalysis. Never raises — returns UNKNOWN on data gaps.
        """
        log.info("fee_analyze_start", token=token_address, hours=hours)

        pair = await self._fetch_best_pair(token_address)
        if pair is None:
            log.warning("fee_no_pair", token=token_address)
            return self._unknown("No DEX pair data found for this token.")

        result = self._compute_analysis(pair, hours)

        log.info(
            "fee_analyze_done",
            token=token_address,
            label=result.label,
            ratio=result.fee_volume_ratio,
            score=result.score_adjustment,
        )
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _fetch_best_pair(self, token_address: str) -> dict | None:
        """Fetch the highest-liquidity Solana pair for the token."""
        try:
            return await self._dex.get_top_pair_for_token(token_address, chain="solana")
        except Exception as exc:  # noqa: BLE001
            log.warning("fee_dexscreener_error", token=token_address, error=str(exc))
            return None

    def _compute_analysis(self, pair: dict, hours: int) -> FeeAnalysis:
        """
        Derive FeeAnalysis from a DexScreener pair dict.

        Volume keys: pair["volume"]["h1"] or pair["volume"]["h24"]
        Fee keys:    pair["fees"]["h1"]   or pair["fees"]["h24"]
                     (present only for some DEXes/pairs)
        """
        window = "h1" if hours <= 1 else "h24"

        volume_data = pair.get("volume") or {}
        volume_usd = float(volume_data.get(window, 0.0) or 0.0)

        if volume_usd <= 0:
            return self._unknown(
                f"No volume data for window '{window}'. Cannot assess fee ratio."
            )

        # Attempt to read actual fees from pair data
        fees_data = pair.get("fees") or {}
        actual_fee = fees_data.get(window)

        if actual_fee is None:
            # DexScreener doesn't expose fees for this pair/window
            # We can still compute expected fee for context but cannot classify
            expected = volume_usd * _STANDARD_LP_FEE
            return self._unknown(
                f"Volume={volume_usd:.0f} USD, expected_fee≈{expected:.0f} USD — "
                f"but actual fee not reported by DexScreener for this pair."
            )

        actual_fee_f = float(actual_fee or 0.0)
        expected_fee = volume_usd * _STANDARD_LP_FEE

        if expected_fee <= 0:
            return self._unknown("Expected fee is zero — cannot compute ratio.")

        ratio = actual_fee_f / expected_fee
        label = _classify_ratio(ratio)
        adj = _SCORE_MAP[label]

        reasoning = self._build_reasoning(label, volume_usd, actual_fee_f, expected_fee, ratio, window)

        return FeeAnalysis(
            label=label,
            fee_volume_ratio=ratio,
            score_adjustment=adj,
            reasoning=reasoning,
        )

    @staticmethod
    def _build_reasoning(
        label: str,
        volume: float,
        actual_fee: float,
        expected_fee: float,
        ratio: float,
        window: str,
    ) -> str:
        return (
            f"[{window}] Volume=${volume:,.0f} | "
            f"Expected fee (0.25%)=${expected_fee:,.0f} | "
            f"Actual fee=${actual_fee:,.0f} | "
            f"Ratio={ratio:.2f} → {label}"
        )

    @staticmethod
    def _unknown(reason: str) -> FeeAnalysis:
        return FeeAnalysis(
            label="UNKNOWN",
            fee_volume_ratio=0.0,
            score_adjustment=0.0,
            reasoning=reason,
        )
