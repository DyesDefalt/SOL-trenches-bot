"""
Signal Scoring Engine — convert token data → score 0-100.

Formula sesuai spec dengan weight bobot:
- Smart Money Count: 35%
- MCAP & "Sedang di Bawah": 20%
- Volume & Momentum: 15%
- Liquidity & Fees: 10%
- Security Score: 10%
- KOL/Social: 5%
- Penalty Bundle/Insider: -10%

Threshold:
- Score ≥ 75 → AUTO BUY
- Score 65-74 → ALERT (manual review)
- Score < 65 → SKIP

Dipakai di:
- Backtester (Phase 2): replay historical data, score offline
- Live Signal Engine (Phase 3): real-time scoring tiap 30 detik

Engine ini DETERMINISTIC dan PURE FUNCTION — sama input → sama output.
Tidak ada randomness, tidak ada I/O di dalam scoring (semua data di-pass via TokenData).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from src.config import settings
from src.infra.logger import get_logger

log = get_logger(__name__)

ScoreAction = Literal["BUY", "ALERT", "SKIP", "REJECT"]


@dataclass
class TokenData:
    """
    Snapshot data 1 token untuk scoring. Semua field optional kecuali address.

    Diisi oleh data layer (Phase 1 clients) atau backtester replay engine.
    """

    address: str
    symbol: str = ""
    name: str = ""

    # Market data
    mcap_usd: float = 0.0
    liquidity_usd: float = 0.0
    price_usd: float = 0.0
    price_ath: float = 0.0  # all-time high price (untuk "sedang di bawah" check)
    age_minutes: int = 0  # umur token sejak launch

    # Volume momentum
    volume_5m_usd: float = 0.0
    volume_15m_usd: float = 0.0
    volume_1h_usd: float = 0.0
    volume_increasing: bool = False  # volume_5m > volume_15m/3 (proxy untuk uptrend)

    # Smart money signals
    smart_money_count: int = 0  # jumlah A+B tier wallet beli dalam window
    smart_money_buyers: list[str] = field(default_factory=list)  # addresses
    kol_count: int = 0

    # Security
    is_honeypot: bool = False
    lp_burned: bool = False
    is_renounced: bool = False
    gmgn_security_score: int = 0  # 0-100 from GMGN
    dev_holding_pct: float = 0.0
    bundle_supply_pct: float = 0.0  # % supply bought in bundles at launch
    insider_count: int = 0

    # Optional context
    holder_count: int = 0
    top10_holders_pct: float = 0.0

    # Phase 7d-7f: extended signal fields
    smart_money_trend: str = "unknown"
    # Nansen: sustained_accumulation, fresh_entry, reducing, distribution, mixed, unknown
    smart_money_composite_bonus: float = 0.0  # -30 to +30, dari SmartMoneyAggregator
    cluster_signal_strength: str = "NONE"     # NONE, WEAK, MEDIUM, STRONG, VERY_STRONG
    pumpfun_graduation_pct: float = 0.0
    pumpfun_score_bonus: float = 0.0
    multi_source_safety_score: float = 1.0    # 0-1, dari TokenVerifier weighted_safety_score
    multi_source_critical_flags: list[str] = field(default_factory=list)

    # Phase 9: Macro regime + news + cross-reference
    macro_regime_level: str = "neutral"        # risk_on, neutral, risk_off, extreme_risk_off
    macro_position_multiplier: float = 1.0     # 0.0-1.5, applied to position size
    macro_skip_entries: bool = False           # hard skip flag (extreme_risk_off)
    narrative_match: bool = False              # ticker matches trending narrative
    narrative_bonus: float = 0.0               # -10 to +10, news/sentiment scoring
    crossref_bonus: float = 0.0                # -5 to +15, CoinGecko+Messari listing
    is_listed_on_coingecko: bool = False
    coingecko_rank: int | None = None
    is_listed_on_messari: bool = False
    fud_detected: bool = False
    fud_severity: str = ""                     # "high", "medium", "low", ""


@dataclass
class ScoreBreakdown:
    """Detailed breakdown untuk debugging + Telegram alert."""

    smart_money: float = 0.0
    mcap_position: float = 0.0
    volume_momentum: float = 0.0
    liquidity_fees: float = 0.0
    security: float = 0.0
    kol_social: float = 0.0
    bundle_penalty: float = 0.0
    other_penalty: float = 0.0

    # Phase 7d-7f: bonus fields dari new signal sources
    smart_money_trend_bonus: float = 0.0
    cluster_signal_bonus: float = 0.0
    pumpfun_bonus: float = 0.0

    # Phase 9: narrative + cross-reference bonuses
    narrative_bonus: float = 0.0
    crossref_bonus: float = 0.0

    def total(self) -> float:
        return (
            self.smart_money
            + self.mcap_position
            + self.volume_momentum
            + self.liquidity_fees
            + self.security
            + self.kol_social
            + self.bundle_penalty
            + self.other_penalty
            + self.smart_money_trend_bonus
            + self.cluster_signal_bonus
            + self.pumpfun_bonus
            + self.narrative_bonus
            + self.crossref_bonus
        )


@dataclass
class ScoreResult:
    """Full scoring result dengan action recommendation."""

    token: TokenData
    score: float
    action: ScoreAction
    reject_reasons: list[str] = field(default_factory=list)
    breakdown: ScoreBreakdown = field(default_factory=ScoreBreakdown)

    def to_dict(self) -> dict:
        return {
            "address": self.token.address,
            "symbol": self.token.symbol,
            "score": round(self.score, 2),
            "action": self.action,
            "reject_reasons": self.reject_reasons,
            "breakdown": {
                "smart_money": round(self.breakdown.smart_money, 2),
                "mcap_position": round(self.breakdown.mcap_position, 2),
                "volume_momentum": round(self.breakdown.volume_momentum, 2),
                "liquidity_fees": round(self.breakdown.liquidity_fees, 2),
                "security": round(self.breakdown.security, 2),
                "kol_social": round(self.breakdown.kol_social, 2),
                "bundle_penalty": round(self.breakdown.bundle_penalty, 2),
                "other_penalty": round(self.breakdown.other_penalty, 2),
                "smart_money_trend_bonus": round(self.breakdown.smart_money_trend_bonus, 2),
                "cluster_signal_bonus": round(self.breakdown.cluster_signal_bonus, 2),
                "pumpfun_bonus": round(self.breakdown.pumpfun_bonus, 2),
                "narrative_bonus": round(self.breakdown.narrative_bonus, 2),
                "crossref_bonus": round(self.breakdown.crossref_bonus, 2),
            },
            "context": {
                "mcap_usd": self.token.mcap_usd,
                "liquidity_usd": self.token.liquidity_usd,
                "smart_money_count": self.token.smart_money_count,
                "volume_5m_usd": self.token.volume_5m_usd,
                "gmgn_security_score": self.token.gmgn_security_score,
                "bundle_supply_pct": self.token.bundle_supply_pct,
                "smart_money_trend": self.token.smart_money_trend,
                "cluster_signal_strength": self.token.cluster_signal_strength,
                "pumpfun_graduation_pct": self.token.pumpfun_graduation_pct,
                "multi_source_safety_score": self.token.multi_source_safety_score,
                "multi_source_critical_flags": self.token.multi_source_critical_flags,
                "macro_regime_level": self.token.macro_regime_level,
                "macro_position_multiplier": self.token.macro_position_multiplier,
                "narrative_match": self.token.narrative_match,
                "is_listed_on_coingecko": self.token.is_listed_on_coingecko,
                "coingecko_rank": self.token.coingecko_rank,
                "fud_detected": self.token.fud_detected,
                "fud_severity": self.token.fud_severity,
            },
        }


class ScoringEngine:
    """
    Pure-function scoring. No I/O, no async, no side effects.

    Konfigurasi di-pull dari `settings` (config.py) — bisa di-tune via env vars.
    Untuk backtester yang ingin test berbagai threshold, instantiate engine baru
    dengan override params.
    """

    def __init__(
        self,
        # Filter thresholds (hard reject)
        max_mcap_usd: float | None = None,
        min_liquidity_usd: float | None = None,
        min_security_score: int | None = None,
        max_dev_holding_pct: float | None = None,
        max_bundle_supply_pct: float | None = None,
        # Scoring weights (override default supaya bisa ekspeimen)
        weight_smart_money: int | None = None,
        weight_mcap_position: int | None = None,
        weight_volume_momentum: int | None = None,
        weight_liquidity: int | None = None,
        weight_security: int | None = None,
        weight_kol_social: int | None = None,
        penalty_bundle: int | None = None,
        # Action thresholds
        min_score_buy: int | None = None,
        min_score_alert: int | None = None,
    ) -> None:
        # Filter
        self.max_mcap_usd = max_mcap_usd if max_mcap_usd is not None else settings.filter_max_mcap_usd
        self.min_liquidity_usd = (
            min_liquidity_usd if min_liquidity_usd is not None else settings.filter_min_liquidity_usd
        )
        self.min_security_score = (
            min_security_score if min_security_score is not None else settings.filter_min_gmgn_security_score
        )
        self.max_dev_holding_pct = (
            max_dev_holding_pct if max_dev_holding_pct is not None else settings.filter_max_dev_holding_pct
        )
        self.max_bundle_supply_pct = (
            max_bundle_supply_pct if max_bundle_supply_pct is not None else settings.filter_max_bundle_supply_pct
        )

        # Weights (NOTE: bukan persen, tapi point max untuk komponen)
        self.weight_smart_money = (
            weight_smart_money if weight_smart_money is not None else settings.score_weight_smart_money
        )
        self.weight_mcap_position = (
            weight_mcap_position if weight_mcap_position is not None else settings.score_weight_mcap_position
        )
        self.weight_volume_momentum = (
            weight_volume_momentum if weight_volume_momentum is not None else settings.score_weight_volume_momentum
        )
        self.weight_liquidity = (
            weight_liquidity if weight_liquidity is not None else settings.score_weight_liquidity
        )
        self.weight_security = (
            weight_security if weight_security is not None else settings.score_weight_security
        )
        self.weight_kol_social = (
            weight_kol_social if weight_kol_social is not None else settings.score_weight_kol_social
        )
        self.penalty_bundle = (
            penalty_bundle if penalty_bundle is not None else settings.score_penalty_bundle
        )

        # Action thresholds
        self.min_score_buy = min_score_buy if min_score_buy is not None else settings.min_score_to_buy
        self.min_score_alert = (
            min_score_alert if min_score_alert is not None else settings.min_score_to_alert
        )

    # ------------------------------------------------------------------
    # Hard-reject filter
    # ------------------------------------------------------------------
    def _check_hard_filters(self, t: TokenData) -> list[str]:
        """Return list of reasons untuk reject. Empty = passes filter."""
        reasons: list[str] = []

        # Phase 9: Macro regime hard skip — extreme risk-off blocks all new entries
        if t.macro_skip_entries:
            reasons.append(f"macro_extreme_risk_off (regime={t.macro_regime_level})")

        # Phase 9: High-severity FUD detected (hack/exploit/SEC lawsuit) → veto
        if t.fud_detected and t.fud_severity == "high":
            reasons.append("fud_event_high_severity")

        if t.mcap_usd > self.max_mcap_usd:
            reasons.append(f"mcap_too_high (${t.mcap_usd:.0f} > ${self.max_mcap_usd:.0f})")
        if t.liquidity_usd < self.min_liquidity_usd:
            reasons.append(f"liquidity_too_low (${t.liquidity_usd:.0f} < ${self.min_liquidity_usd:.0f})")

        # Multi-source critical flags check (Phase 7d) — primary path
        critical_flags = t.multi_source_critical_flags or []
        if "honeypot" in critical_flags:
            reasons.append("honeypot_detected")
        if "lp_unlocked" in critical_flags:
            reasons.append("lp_unlocked_detected")
        if "mint_not_renounced" in critical_flags:
            reasons.append("mint_not_renounced_detected")

        # Backward compat: fallback ke legacy is_honeypot field kalau multi_source tidak diisi
        if t.is_honeypot and "honeypot_detected" not in reasons:
            reasons.append("honeypot_detected")

        if t.gmgn_security_score and t.gmgn_security_score < self.min_security_score:
            reasons.append(f"security_score_low ({t.gmgn_security_score} < {self.min_security_score})")
        if t.dev_holding_pct > self.max_dev_holding_pct:
            reasons.append(f"dev_holding_too_high ({t.dev_holding_pct:.1f}% > {self.max_dev_holding_pct:.1f}%)")
        # Bundle adalah penalty di scoring, TAPI kalau ekstrem → hard reject
        if t.bundle_supply_pct > self.max_bundle_supply_pct:
            reasons.append(f"bundle_too_extreme ({t.bundle_supply_pct:.1f}% > {self.max_bundle_supply_pct:.1f}%)")
        return reasons

    # ------------------------------------------------------------------
    # Component scorers (pure)
    # ------------------------------------------------------------------
    def _score_smart_money(self, t: TokenData) -> float:
        """
        Smart money count → max weight_smart_money points.
        Per spec: score += min(sm_count * 12, 40) untuk weight=35
        Generalize: per-wallet contribution = weight_smart_money / ~3 (3 wallet → max).
        """
        if t.smart_money_count <= 0:
            return 0.0
        # 1 wallet → ~12, 2 → ~24, 3+ → max
        per_wallet = self.weight_smart_money / 3
        return min(t.smart_money_count * per_wallet, float(self.weight_smart_money))

    def _score_mcap_position(self, t: TokenData) -> float:
        """
        Low MCAP + harga "sedang di bawah" → max weight_mcap_position points.

        Per spec:
            if mcap < 25000: score += 15
            if price_distance_from_ath > 40%: score += 10  ("sedang di bawah")

        Generalize:
            - Low MCAP component: scale linear sampai 25k = full points
            - Below-ATH component: ≥40% drawdown = full points
        """
        score = 0.0

        # Low MCAP component (60% of weight)
        low_mcap_max = self.weight_mcap_position * 0.6
        if t.mcap_usd > 0:
            if t.mcap_usd <= 10_000:
                score += low_mcap_max
            elif t.mcap_usd <= 25_000:
                # Linear interpolate: 25k → 0.5 of max, 10k → full max
                ratio = 1 - (t.mcap_usd - 10_000) / 15_000 * 0.5
                score += low_mcap_max * ratio
            elif t.mcap_usd <= self.max_mcap_usd:
                # 25k-60k: small contribution
                ratio = max(0, 1 - (t.mcap_usd - 25_000) / (self.max_mcap_usd - 25_000))
                score += low_mcap_max * ratio * 0.3

        # "Sedang di bawah" / below-ATH component (40% of weight)
        below_ath_max = self.weight_mcap_position * 0.4
        if t.price_ath > 0 and t.price_usd > 0:
            distance = (t.price_ath - t.price_usd) / t.price_ath
            if distance >= 0.4:
                score += below_ath_max
            elif distance >= 0.2:
                # Partial: 20-40% drawdown
                score += below_ath_max * (distance - 0.2) / 0.2

        return min(score, float(self.weight_mcap_position))

    def _score_volume_momentum(self, t: TokenData) -> float:
        """
        Volume bagus + naik = max weight_volume_momentum.

        Per spec: if vol_5m > 3000 and vol_increasing: score += 12 (untuk weight=15)

        Generalize:
            - Volume threshold component: vol_5m ≥ 3k → 80% of weight
            - Increasing component: vol_increasing flag → 20% of weight
        """
        score = 0.0
        threshold_max = self.weight_volume_momentum * 0.8
        increasing_max = self.weight_volume_momentum * 0.2

        if t.volume_5m_usd >= 5_000:
            score += threshold_max
        elif t.volume_5m_usd >= 3_000:
            score += threshold_max * 0.7
        elif t.volume_5m_usd >= 1_000:
            score += threshold_max * 0.4

        if t.volume_increasing:
            score += increasing_max

        return min(score, float(self.weight_volume_momentum))

    def _score_liquidity(self, t: TokenData) -> float:
        """
        Liquidity yang sehat + fee rendah → max weight_liquidity.

        Logic: liquidity ≥ 20k = full points, scale linear dari min ke 20k.
        """
        if t.liquidity_usd < self.min_liquidity_usd:
            return 0.0
        if t.liquidity_usd >= 20_000:
            return float(self.weight_liquidity)
        # Linear scale dari min_liquidity → 20k
        ratio = (t.liquidity_usd - self.min_liquidity_usd) / (20_000 - self.min_liquidity_usd)
        return float(self.weight_liquidity) * ratio

    def _score_security(self, t: TokenData) -> float:
        """
        Security clean → max weight_security.

        Per spec: if lp_burned and renounced and not_honeypot: score += 10 (weight=10)

        Phase 7d: kalau multi_source_safety_score tersedia (> 0), gunakan itu sebagai primary.
        Fallback ke legacy GMGN score + lp_burned + is_renounced kalau multi_source belum diisi.
        """
        if t.is_honeypot:
            return 0.0  # Already hard-reject, but defensive

        # Phase 7d: multi-source safety score overrides legacy scoring
        if t.multi_source_safety_score > 0:
            return float(self.weight_security) * t.multi_source_safety_score

        # Legacy fallback (backward compat)
        score = 0.0
        # GMGN score component (50% of weight)
        if t.gmgn_security_score:
            score += self.weight_security * 0.5 * (t.gmgn_security_score / 100)
        # LP burned (25%)
        if t.lp_burned:
            score += self.weight_security * 0.25
        # Renounced (25%)
        if t.is_renounced:
            score += self.weight_security * 0.25
        return min(score, float(self.weight_security))

    def _score_kol_social(self, t: TokenData) -> float:
        """KOL mention bonus."""
        if t.kol_count <= 0:
            return 0.0
        return min(t.kol_count * (self.weight_kol_social / 2), float(self.weight_kol_social))

    # ------------------------------------------------------------------
    # Phase 7d-7f: new signal scorers
    # ------------------------------------------------------------------

    def _score_smart_money_trend(self, t: TokenData) -> float:
        """
        Smart money trend bonus dari Nansen / SmartMoneyAggregator composite_score_bonus.

        Apply composite_bonus langsung (sudah -30..+30), clamp ke ±30.
        Trend label (t.smart_money_trend) dipakai untuk logging / breakdown saja;
        actual magnitude dari composite_bonus.
        """
        bonus = max(-30.0, min(30.0, t.smart_money_composite_bonus))
        return bonus

    def _score_cluster_signal(self, t: TokenData) -> float:
        """
        Cluster signal dari ClusterDetector.

        VERY_STRONG: +20
        STRONG: +15
        MEDIUM: +5
        WEAK / NONE: 0
        """
        strength = (t.cluster_signal_strength or "NONE").upper()
        if strength == "VERY_STRONG":
            return 20.0
        if strength == "STRONG":
            return 15.0
        if strength == "MEDIUM":
            return 5.0
        # WEAK atau NONE
        return 0.0

    def _score_pumpfun(self, t: TokenData) -> float:
        """
        Pump.fun graduation bonus, sudah dihitung oleh PumpfunTracker.
        Return as-is — tracker yang handle semua logic.
        """
        return float(t.pumpfun_score_bonus)

    # ------------------------------------------------------------------
    # Phase 9: new signal scorers (narrative + cross-reference)
    # ------------------------------------------------------------------

    def _score_narrative(self, t: TokenData) -> float:
        """
        Narrative & sentiment bonus dari NewsAggregator.

        Clamp -10 to +10. Already computed upstream — return as-is.
        Medium-severity FUD penalty applied here (high goes to hard reject above).
        """
        bonus = max(-10.0, min(10.0, t.narrative_bonus))
        if t.fud_detected and t.fud_severity == "medium":
            bonus -= 5.0
        return max(-10.0, bonus)

    def _score_crossref(self, t: TokenData) -> float:
        """
        Cross-reference validation bonus dari CrossRefValidator.

        Clamp -5 to +15.
        """
        return max(-5.0, min(15.0, t.crossref_bonus))

    def _penalty_bundle(self, t: TokenData) -> float:
        """Bundle/insider penalty (negative score)."""
        if t.bundle_supply_pct <= 5:
            return 0.0
        if t.bundle_supply_pct >= 25:
            return float(self.penalty_bundle)  # full penalty
        # Linear scale 5%-25%
        ratio = (t.bundle_supply_pct - 5) / 20
        return self.penalty_bundle * ratio

    # ------------------------------------------------------------------
    # Public scoring API
    # ------------------------------------------------------------------
    def score(self, token: TokenData) -> ScoreResult:
        """
        Score token. Returns ScoreResult dengan total score, action, breakdown.
        """
        # Hard filter check first
        reject_reasons = self._check_hard_filters(token)
        if reject_reasons:
            return ScoreResult(
                token=token,
                score=0.0,
                action="REJECT",
                reject_reasons=reject_reasons,
                breakdown=ScoreBreakdown(),
            )

        breakdown = ScoreBreakdown(
            smart_money=self._score_smart_money(token),
            mcap_position=self._score_mcap_position(token),
            volume_momentum=self._score_volume_momentum(token),
            liquidity_fees=self._score_liquidity(token),
            security=self._score_security(token),
            kol_social=self._score_kol_social(token),
            bundle_penalty=self._penalty_bundle(token),
            other_penalty=0.0,
            # Phase 7d-7f: new signal bonuses
            smart_money_trend_bonus=self._score_smart_money_trend(token),
            cluster_signal_bonus=self._score_cluster_signal(token),
            pumpfun_bonus=self._score_pumpfun(token),
            # Phase 9: narrative + cross-reference bonuses
            narrative_bonus=self._score_narrative(token),
            crossref_bonus=self._score_crossref(token),
        )

        total = breakdown.total()
        # Clamp 0-100
        total = max(0.0, min(100.0, total))

        if total >= self.min_score_buy:
            action: ScoreAction = "BUY"
        elif total >= self.min_score_alert:
            action = "ALERT"
        else:
            action = "SKIP"

        return ScoreResult(
            token=token,
            score=total,
            action=action,
            reject_reasons=[],
            breakdown=breakdown,
        )

    def position_size_sol(self, score: float, macro_multiplier: float = 1.0) -> float:
        """
        Confidence-adjusted position sizing (Kelly fractional).

        Score 75-79: 0.015 SOL (low confidence)
        Score 80-84: 0.025 SOL (medium)
        Score 85-89: 0.035 SOL (high)
        Score 90+:   0.050 SOL (very high)

        Phase 9: macro_multiplier (0.0-1.5) applied at the end.
        - risk_on (1.3): boosts size up to 0.065 SOL on score 90+
        - neutral (1.0): no change (default for backward compat)
        - risk_off (0.5): cuts size in half
        - extreme_risk_off (0.0): zero — caller should hard-skip
        Floor at 0.005 SOL minimum (or 0 if multiplier=0).
        """
        if score >= 90:
            base = 0.050
        elif score >= 85:
            base = 0.035
        elif score >= 80:
            base = 0.025
        elif score >= 75:
            base = 0.015
        else:
            return 0.0  # below buy threshold

        # Clamp multiplier to safe range
        mult = max(0.0, min(1.5, macro_multiplier))
        sized = base * mult

        # Below min effective size → skip (avoid micro-trades eating fees)
        if sized < 0.005:
            return 0.0
        # Cap at hard ceiling per env (safety)
        return min(sized, settings.max_position_size_sol)
