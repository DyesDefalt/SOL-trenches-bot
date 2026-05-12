"""
Macro Regime Detector — Phase 9.

Combines CryptoQuant (BTC on-chain) + Alpha Vantage (SPX, DXY, VIX) signals
to classify the macro environment for position sizing throttling.

Regime levels (from most bearish to most bullish):
  EXTREME_RISK_OFF → skip all new entries, multiplier=0.0
  RISK_OFF         → halve position sizes, multiplier=0.5
  NEUTRAL          → normal sizing, multiplier=1.0
  RISK_ON          → allow larger positions, multiplier=1.3

Used by scoring engine to scale position_size_multiplier before trade execution.

Graceful degradation:
  - If either client is None or returns no data, missing signals are treated as neutral.
  - Regime always returns a valid MacroRegime (defaults to NEUTRAL with multiplier=1.0).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from src.infra.cache import cached
from src.infra.logger import get_logger

if TYPE_CHECKING:
    from src.clients.alphavantage_client import AlphaVantageClient
    from src.clients.cryptoquant_client import CryptoQuantClient

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class RegimeLevel(str, Enum):
    RISK_ON = "risk_on"                    # crypto bullish, macro bullish
    NEUTRAL = "neutral"
    RISK_OFF = "risk_off"                  # macro bearish, BTC down hard
    EXTREME_RISK_OFF = "extreme_risk_off"  # rare emergency state


@dataclass
class MacroRegime:
    """
    Macro market regime snapshot.

    position_size_multiplier: factor applied to base position size (0.0–1.5).
    should_skip_entries:      True means do not open new positions.
    reasons:                  human-readable explanation of classification.
    """

    level: RegimeLevel
    btc_24h_change_pct: float = 0.0
    spx_change_pct: float = 0.0
    dxy_change_pct: float = 0.0
    vix_value: float = 0.0
    mvrv_ratio: float = 0.0
    funding_rate_avg: float = 0.0
    position_size_multiplier: float = 1.0  # 0.0–1.5
    should_skip_entries: bool = False
    reasons: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helper: extract floats from API payloads
# ---------------------------------------------------------------------------

def _latest_value(data: dict, field_name: str) -> float | None:
    """
    Extract most recent value for `field_name` from a normalized CryptoQuant response.

    CryptoQuant normalized shape: {"data": [{"date": ..., <field_name>: ...}, ...]}
    Returns float or None if not available.
    """
    entries = data.get("data", [])
    if not entries or not isinstance(entries, list):
        return None
    # Last entry is most recent
    last = entries[-1]
    if not isinstance(last, dict):
        return None
    val = last.get(field_name)
    try:
        return float(val) if val is not None else None
    except (ValueError, TypeError):
        return None


def _avg_field(data: dict, field_name: str) -> float | None:
    """Average of `field_name` across all data points. Returns None if no data."""
    entries = data.get("data", [])
    if not entries or not isinstance(entries, list):
        return None
    values: list[float] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        val = entry.get(field_name)
        try:
            values.append(float(val))
        except (ValueError, TypeError):
            continue
    return sum(values) / len(values) if values else None


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

class MacroRegimeDetector:
    """
    Detects macro market regime using CryptoQuant + Alpha Vantage data.

    Constructor accepts both clients as optional — if either is None,
    signals from that source are skipped and treated as neutral.

    Usage::

        detector = MacroRegimeDetector(
            cryptoquant=CryptoQuantClient(),
            alphavantage=AlphaVantageClient(),
        )
        regime = await detector.detect_regime()
        if regime.should_skip_entries:
            log.info("macro_skip", reason=regime.reasons)
    """

    def __init__(
        self,
        cryptoquant: "CryptoQuantClient | None" = None,
        alphavantage: "AlphaVantageClient | None" = None,
    ) -> None:
        self._cq = cryptoquant
        self._av = alphavantage

    @cached(prefix="macro_regime", ttl=300)
    async def detect_regime(self) -> MacroRegime:
        """
        Fetch all macro signals and classify the current regime.

        Returns a MacroRegime — always succeeds (defaults to NEUTRAL on failure).
        Cached for 5 minutes to avoid hammering rate-limited APIs.
        """
        log.info("macro_regime_detection_start")

        # Fetch all signals in parallel; gracefully handle None clients
        (
            cq_funding,
            cq_mvrv,
            av_spx,
            av_dxy,
            av_vix,
            av_btc,
        ) = await asyncio.gather(
            self._safe_cq_funding(),
            self._safe_cq_mvrv(),
            self._safe_av_spx(),
            self._safe_av_dxy(),
            self._safe_av_vix(),
            self._safe_av_btc(),
            return_exceptions=False,  # _safe_* methods never raise
        )

        # --- Extract individual signal values ---
        btc_24h_change_pct = self._calc_btc_24h_change(av_btc)
        spx_change_pct = float(av_spx.get("change_pct", 0.0)) if av_spx else 0.0
        dxy_change_pct = float(av_dxy.get("change_pct", 0.0)) if av_dxy else 0.0
        vix_value = float(av_vix.get("price", 0.0)) if av_vix else 0.0
        vix_change_pct = float(av_vix.get("change_pct", 0.0)) if av_vix else 0.0

        mvrv_ratio = _latest_value(cq_mvrv, "mvrv_ratio") or 0.0
        funding_rate_avg = _avg_field(cq_funding, "funding_rate") or 0.0

        # --- Classify regime ---
        regime, reasons = self._classify(
            btc_24h_change_pct=btc_24h_change_pct,
            spx_change_pct=spx_change_pct,
            dxy_change_pct=dxy_change_pct,
            vix_value=vix_value,
            vix_change_pct=vix_change_pct,
        )

        multiplier = self._multiplier_for_regime(regime)
        skip_entries = regime == RegimeLevel.EXTREME_RISK_OFF

        result = MacroRegime(
            level=regime,
            btc_24h_change_pct=btc_24h_change_pct,
            spx_change_pct=spx_change_pct,
            dxy_change_pct=dxy_change_pct,
            vix_value=vix_value,
            mvrv_ratio=mvrv_ratio,
            funding_rate_avg=funding_rate_avg,
            position_size_multiplier=multiplier,
            should_skip_entries=skip_entries,
            reasons=reasons,
        )

        log.info(
            "macro_regime_detected",
            level=regime.value,
            btc_24h=btc_24h_change_pct,
            spx=spx_change_pct,
            dxy=dxy_change_pct,
            vix=vix_value,
            multiplier=multiplier,
            skip=skip_entries,
            reasons=reasons,
        )

        return result

    # ------------------------------------------------------------------
    # Classification logic
    # ------------------------------------------------------------------

    def _classify(
        self,
        btc_24h_change_pct: float,
        spx_change_pct: float,
        dxy_change_pct: float,
        vix_value: float,
        vix_change_pct: float,
    ) -> tuple[RegimeLevel, list[str]]:
        """
        Classify regime level based on signal thresholds.

        Priority order (most extreme checked first):
          1. EXTREME_RISK_OFF: BTC -10%+ OR (DXY +1.5% AND SPX -2%+)
          2. RISK_OFF:          BTC -5%+ OR SPX -1.5%+ OR VIX rising sharply
          3. RISK_ON:           BTC +5%+ AND (SPX flat-positive OR DXY weak)
          4. NEUTRAL:           default fallback
        """
        reasons: list[str] = []

        # --- EXTREME_RISK_OFF checks ---
        if btc_24h_change_pct <= -10.0:
            reasons.append(f"BTC crashed {btc_24h_change_pct:.1f}% in 24h")
            return RegimeLevel.EXTREME_RISK_OFF, reasons

        if dxy_change_pct >= 1.5 and spx_change_pct <= -2.0:
            reasons.append(
                f"DXY surging +{dxy_change_pct:.1f}% while SPX down {spx_change_pct:.1f}%"
            )
            return RegimeLevel.EXTREME_RISK_OFF, reasons

        # --- RISK_OFF checks ---
        risk_off_triggers: list[str] = []

        if btc_24h_change_pct <= -5.0:
            risk_off_triggers.append(f"BTC down {btc_24h_change_pct:.1f}% in 24h")

        if spx_change_pct <= -1.5:
            risk_off_triggers.append(f"SPX down {spx_change_pct:.1f}%")

        # VIX "rising sharply" — use >20 absolute OR >10% single-day spike as proxy
        # (VIXY price, not raw VIX, so thresholds adjusted)
        if vix_value > 0 and vix_change_pct >= 10.0:
            risk_off_triggers.append(
                f"VIX (VIXY) spiking +{vix_change_pct:.1f}% today"
            )

        if risk_off_triggers:
            reasons.extend(risk_off_triggers)
            return RegimeLevel.RISK_OFF, reasons

        # --- RISK_ON checks ---
        spx_ok = spx_change_pct >= 0.0  # flat or positive
        dxy_weak = dxy_change_pct <= 0.0  # DXY flat or falling = dollar weak

        if btc_24h_change_pct >= 5.0 and (spx_ok or dxy_weak):
            reasons.append(f"BTC up {btc_24h_change_pct:.1f}% in 24h with supportive macro")
            if spx_ok:
                reasons.append(f"SPX {spx_change_pct:+.1f}% (flat/positive)")
            if dxy_weak:
                reasons.append(f"DXY {dxy_change_pct:+.1f}% (weak dollar)")
            return RegimeLevel.RISK_ON, reasons

        # --- NEUTRAL (default) ---
        reasons.append("No strong macro signal — defaulting to neutral")
        return RegimeLevel.NEUTRAL, reasons

    @staticmethod
    def _multiplier_for_regime(level: RegimeLevel) -> float:
        """Map regime level to position size multiplier. Capped [0.0, 1.5]."""
        multipliers = {
            RegimeLevel.EXTREME_RISK_OFF: 0.0,
            RegimeLevel.RISK_OFF: 0.5,
            RegimeLevel.NEUTRAL: 1.0,
            RegimeLevel.RISK_ON: 1.3,
        }
        raw = multipliers.get(level, 1.0)
        return max(0.0, min(1.5, raw))

    # ------------------------------------------------------------------
    # BTC 24h change from Alpha Vantage daily candles
    # ------------------------------------------------------------------

    @staticmethod
    def _calc_btc_24h_change(btc_daily: dict) -> float:
        """
        Extract approximate 24h BTC price change from Alpha Vantage daily candles.

        Returns pct change from previous close to latest close, or 0.0 if unavailable.
        """
        if not btc_daily:
            return 0.0
        ts_key = "Time Series (Digital Currency Daily)"
        ts = btc_daily.get(ts_key, {})
        if not ts or not isinstance(ts, dict):
            return 0.0

        dates = sorted(ts.keys(), reverse=True)
        if len(dates) < 2:
            return 0.0

        try:
            latest_close = float(ts[dates[0]].get("4a. close (USD)", 0.0))
            prev_close = float(ts[dates[1]].get("4a. close (USD)", 0.0))
            if prev_close == 0.0:
                return 0.0
            return ((latest_close - prev_close) / prev_close) * 100.0
        except (ValueError, TypeError, KeyError):
            return 0.0

    # ------------------------------------------------------------------
    # Safe fetch wrappers — never raise, return {} on any error
    # ------------------------------------------------------------------

    async def _safe_cq_funding(self) -> dict:
        if self._cq is None:
            return {}
        try:
            return await self._cq.get_btc_funding_rates()
        except Exception as e:
            log.warning("macro_cq_funding_error", error=str(e))
            return {}

    async def _safe_cq_mvrv(self) -> dict:
        if self._cq is None:
            return {}
        try:
            return await self._cq.get_btc_mvrv_ratio()
        except Exception as e:
            log.warning("macro_cq_mvrv_error", error=str(e))
            return {}

    async def _safe_av_spx(self) -> dict:
        if self._av is None:
            return {}
        try:
            return await self._av.get_spx_quote()
        except Exception as e:
            log.warning("macro_av_spx_error", error=str(e))
            return {}

    async def _safe_av_dxy(self) -> dict:
        if self._av is None:
            return {}
        try:
            return await self._av.get_dxy_quote()
        except Exception as e:
            log.warning("macro_av_dxy_error", error=str(e))
            return {}

    async def _safe_av_vix(self) -> dict:
        if self._av is None:
            return {}
        try:
            return await self._av.get_vix_quote()
        except Exception as e:
            log.warning("macro_av_vix_error", error=str(e))
            return {}

    async def _safe_av_btc(self) -> dict:
        if self._av is None:
            return {}
        try:
            return await self._av.get_btc_daily()
        except Exception as e:
            log.warning("macro_av_btc_error", error=str(e))
            return {}
