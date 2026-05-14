"""
Trader Signal Aggregator — orchestrate all 4 degen-trader filters into one signal.

Combines:
  1. BundlerPatternDetector  — multi-wallet rug setup
  2. GlobalFeeAnalyzer       — wash trading via fee mismatch
  3. WalletFundedAnalyzer    — freshly-created sniper wallets
  4. TopHolderBalanceChecker — weak-SOL throwaway holder wallets

Composite score formula:
  score = clamp(
      bundler_adj + fee_adj + funded_adj + balance_adj,
      min=-20, max=+20
  )

  Where each component contributes:
    - BundlerPattern.strength:        CONFIRMED → -20 (hard reject veto)
                                      SUSPICIOUS → -8
                                      NONE → 0
    - FeeAnalysis.score_adjustment:   -10 to +5 (from GlobalFeeAnalyzer)
    - FundedFromAnalysis.score_adj:   -10 to +3 (from WalletFundedAnalyzer)
    - HolderBalanceAnalysis.score_adj:-5 to +5  (from TopHolderBalanceChecker)

  Note: BundlerPattern does NOT have a native score_adjustment field.
  The aggregator applies:  CONFIRMED → -20 (= hard_reject too)
                           SUSPICIOUS → -8
                           NONE → 0

Hard reject veto (hard_reject=True):
  - bundler.strength == "CONFIRMED"     → immediate reject regardless of score
  - fee_analysis.label == "WASH_TRADING" → immediate reject regardless of score
  hard_reject does not block the score calculation — it flags the token for
  the caller's use (integration layer can override entry decision).

All 4 analyzers run in parallel via asyncio.gather.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.infra.logger import get_logger

if TYPE_CHECKING:
    from src.intel.bundler_pattern_detector import BundlerPattern, BundlerPatternDetector
    from src.intel.global_fee_analyzer import FeeAnalysis, GlobalFeeAnalyzer
    from src.intel.top_holder_balance_check import HolderBalanceAnalysis, TopHolderBalanceChecker
    from src.intel.wallet_funded_analyzer import FundedFromAnalysis, WalletFundedAnalyzer

log = get_logger(__name__)

# Score contribution from bundler strength
_BUNDLER_SCORE: dict[str, float] = {
    "NONE": 0.0,
    "SUSPICIOUS": -8.0,
    "CONFIRMED": -20.0,
}

_SCORE_MIN = -20.0
_SCORE_MAX = 20.0


@dataclass
class TraderSignal:
    """
    Composite signal from all 4 degen-trader filters.

    bundler:         multi-wallet bundler pattern result
    fee_analysis:    wash-trading fee analysis
    funded_from:     wallet funding-age analysis
    holder_balance:  top holder SOL balance check

    composite_score: sum of all score_adjustments, clamped [-20, +20]
    hard_reject:     True → token should be rejected regardless of composite_score
    reasoning:       list of per-filter human-readable summaries
    """

    bundler: "BundlerPattern"
    fee_analysis: "FeeAnalysis"
    funded_from: "FundedFromAnalysis"
    holder_balance: "HolderBalanceAnalysis"
    composite_score: float  # -20 to +20
    hard_reject: bool
    reasoning: list[str] = field(default_factory=list)


class TraderSignalAggregator:
    """
    Orchestrate all 4 trader filters and return a unified TraderSignal.

    Constructor:
        bundler_detector:   BundlerPatternDetector
        fee_analyzer:       GlobalFeeAnalyzer
        funded_analyzer:    WalletFundedAnalyzer
        balance_checker:    TopHolderBalanceChecker

    Usage::

        agg = TraderSignalAggregator(bundler, fee, funded, balance)
        signal = await agg.analyze("TokenMintXxx")
        if signal.hard_reject:
            # skip immediately
        score_bonus = signal.composite_score  # add to bot's total score
    """

    def __init__(
        self,
        bundler_detector: "BundlerPatternDetector",
        fee_analyzer: "GlobalFeeAnalyzer",
        funded_analyzer: "WalletFundedAnalyzer",
        balance_checker: "TopHolderBalanceChecker",
    ) -> None:
        self._bundler = bundler_detector
        self._fee = fee_analyzer
        self._funded = funded_analyzer
        self._balance = balance_checker

    async def analyze(
        self,
        token_address: str,
        top_holders: list[str] | None = None,
    ) -> "TraderSignal":
        """
        Run all 4 filters in parallel and return combined TraderSignal.

        top_holders: optional pre-fetched list of holder addresses shared
                     across funded_analyzer and balance_checker to save calls.
        """
        log.info("trader_signal_start", token=token_address)

        # All 4 run concurrently — order is irrelevant
        bundler_result, fee_result, funded_result, balance_result = await asyncio.gather(
            self._bundler.detect(token_address),
            self._fee.analyze(token_address),
            self._funded.analyze(token_address, top_holders=top_holders),
            self._balance.check(token_address, top_holders=top_holders),
            return_exceptions=True,
        )

        # Resolve any exceptions into safe defaults
        bundler_result = _safe_bundler(bundler_result)
        fee_result = _safe_fee(fee_result)
        funded_result = _safe_funded(funded_result)
        balance_result = _safe_balance(balance_result)

        # Composite score
        bundler_score = _BUNDLER_SCORE.get(bundler_result.strength, 0.0)
        raw_score = (
            bundler_score
            + fee_result.score_adjustment
            + funded_result.score_adjustment
            + balance_result.score_adjustment
        )
        composite = max(_SCORE_MIN, min(_SCORE_MAX, raw_score))

        # Hard reject veto
        hard_reject = (
            bundler_result.strength == "CONFIRMED"
            or fee_result.label == "WASH_TRADING"
        )

        # Build reasoning list
        reasons: list[str] = [
            f"Bundler [{bundler_result.strength}]: {bundler_result.reasoning}",
            f"Fee [{fee_result.label}]: {fee_result.reasoning}",
            f"FundedAge [{funded_result.label}]: {funded_result.reasoning}",
            f"HolderSOL [{balance_result.label}]: {balance_result.reasoning}",
        ]

        signal = TraderSignal(
            bundler=bundler_result,
            fee_analysis=fee_result,
            funded_from=funded_result,
            holder_balance=balance_result,
            composite_score=composite,
            hard_reject=hard_reject,
            reasoning=reasons,
        )

        log.info(
            "trader_signal_done",
            token=token_address,
            bundler=bundler_result.strength,
            fee=fee_result.label,
            funded=funded_result.label,
            balance=balance_result.label,
            composite=composite,
            hard_reject=hard_reject,
        )
        return signal


# ---------------------------------------------------------------------------
# Safe default factories — convert exceptions to neutral dataclasses
# ---------------------------------------------------------------------------

def _safe_bundler(result: object) -> "BundlerPattern":
    from src.intel.bundler_pattern_detector import BundlerPattern

    if isinstance(result, Exception):
        log.error("bundler_detector_exception", error=str(result))
        return BundlerPattern(
            strength="NONE",
            reasoning=f"Bundler detector error: {result}",
        )
    return result  # type: ignore[return-value]


def _safe_fee(result: object) -> "FeeAnalysis":
    from src.intel.global_fee_analyzer import FeeAnalysis

    if isinstance(result, Exception):
        log.error("fee_analyzer_exception", error=str(result))
        return FeeAnalysis(
            label="UNKNOWN",
            fee_volume_ratio=0.0,
            score_adjustment=0.0,
            reasoning=f"Fee analyzer error: {result}",
        )
    return result  # type: ignore[return-value]


def _safe_funded(result: object) -> "FundedFromAnalysis":
    from src.intel.wallet_funded_analyzer import FundedFromAnalysis

    if isinstance(result, Exception):
        log.error("funded_analyzer_exception", error=str(result))
        return FundedFromAnalysis(
            label="UNKNOWN",
            median_age_days=None,
            min_age_days=None,
            score_adjustment=0.0,
            reasoning=f"Funded analyzer error: {result}",
        )
    return result  # type: ignore[return-value]


def _safe_balance(result: object) -> "HolderBalanceAnalysis":
    from src.intel.top_holder_balance_check import HolderBalanceAnalysis

    if isinstance(result, Exception):
        log.error("balance_checker_exception", error=str(result))
        return HolderBalanceAnalysis(
            label="UNKNOWN",
            min_balance_sol=None,
            median_balance_sol=None,
            weak_count=0,
            score_adjustment=0.0,
            reasoning=f"Balance checker error: {result}",
        )
    return result  # type: ignore[return-value]
