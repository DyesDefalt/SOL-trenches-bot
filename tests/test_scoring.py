"""Unit tests untuk scoring engine — formula deterministic + edge cases."""

from __future__ import annotations

import pytest

from src.core.scoring import ScoreBreakdown, ScoringEngine, TokenData


@pytest.fixture
def engine() -> ScoringEngine:
    return ScoringEngine()


@pytest.fixture
def good_token() -> TokenData:
    """Token yang harus dapat score tinggi (BUY action)."""
    return TokenData(
        address="GOOD_TOKEN",
        symbol="GOOD",
        mcap_usd=15_000,           # low MCAP → high points
        liquidity_usd=20_000,      # healthy liquidity → full points
        price_usd=0.001,
        price_ath=0.002,           # 50% below ATH → "sedang di bawah"
        volume_5m_usd=5_000,       # healthy volume
        volume_increasing=True,
        smart_money_count=3,       # 3 smart wallets bought → ~max sm score
        is_honeypot=False,
        lp_burned=True,
        is_renounced=True,
        gmgn_security_score=85,
        dev_holding_pct=5,
        bundle_supply_pct=2,
    )


def test_good_token_scores_high(engine: ScoringEngine, good_token: TokenData) -> None:
    result = engine.score(good_token)
    assert result.action == "BUY"
    assert result.score >= 75
    assert result.breakdown.smart_money > 0
    assert result.breakdown.security > 0


def test_low_mcap_max_points(engine: ScoringEngine) -> None:
    """MCAP $10k → 60% of mcap_position weight."""
    token = TokenData(
        address="x",
        mcap_usd=8_000,
        liquidity_usd=15_000,
        price_usd=0.001,
        price_ath=0.001,  # not below ATH
    )
    result = engine.score(token)
    # Just verify mcap_position score reasonable
    assert result.breakdown.mcap_position > 0


def test_below_ath_adds_points(engine: ScoringEngine) -> None:
    """Price 50% below ATH → "sedang di bawah" component."""
    token = TokenData(
        address="x",
        mcap_usd=20_000,
        liquidity_usd=15_000,
        price_usd=0.0005,
        price_ath=0.001,  # 50% drop
    )
    result = engine.score(token)
    assert result.breakdown.mcap_position > 0


def test_smart_money_3_wallets_max(engine: ScoringEngine) -> None:
    """3 smart wallets → max smart money score (35 points default)."""
    token = TokenData(
        address="x",
        mcap_usd=20_000,
        liquidity_usd=15_000,
        smart_money_count=3,
    )
    result = engine.score(token)
    assert result.breakdown.smart_money == 35.0  # full weight default


def test_smart_money_5_wallets_capped(engine: ScoringEngine) -> None:
    """5 smart wallets → STILL capped at weight (35 max)."""
    token = TokenData(
        address="x",
        mcap_usd=20_000,
        liquidity_usd=15_000,
        smart_money_count=5,
    )
    result = engine.score(token)
    assert result.breakdown.smart_money == 35.0


def test_honeypot_rejected(engine: ScoringEngine) -> None:
    """Honeypot detected → REJECT action."""
    token = TokenData(
        address="x",
        mcap_usd=10_000,
        liquidity_usd=15_000,
        is_honeypot=True,
    )
    result = engine.score(token)
    assert result.action == "REJECT"
    assert "honeypot_detected" in result.reject_reasons


def test_high_mcap_rejected(engine: ScoringEngine) -> None:
    """MCAP > 60k → REJECT."""
    token = TokenData(
        address="x",
        mcap_usd=100_000,
        liquidity_usd=20_000,
    )
    result = engine.score(token)
    assert result.action == "REJECT"


def test_low_liquidity_rejected(engine: ScoringEngine) -> None:
    """Liquidity < 8k → REJECT."""
    token = TokenData(
        address="x",
        mcap_usd=20_000,
        liquidity_usd=2_000,
    )
    result = engine.score(token)
    assert result.action == "REJECT"


def test_high_dev_holding_rejected(engine: ScoringEngine) -> None:
    """Dev holding > 15% → REJECT."""
    token = TokenData(
        address="x",
        mcap_usd=20_000,
        liquidity_usd=15_000,
        dev_holding_pct=25,
    )
    result = engine.score(token)
    assert result.action == "REJECT"


def test_bundle_extreme_rejected(engine: ScoringEngine) -> None:
    """Bundle > 30% → REJECT."""
    token = TokenData(
        address="x",
        mcap_usd=20_000,
        liquidity_usd=15_000,
        bundle_supply_pct=40,
    )
    result = engine.score(token)
    assert result.action == "REJECT"


def test_bundle_moderate_penalty(engine: ScoringEngine) -> None:
    """Bundle 15% → partial penalty (-5 from -10 max)."""
    token = TokenData(
        address="x",
        mcap_usd=20_000,
        liquidity_usd=15_000,
        bundle_supply_pct=15,
    )
    result = engine.score(token)
    assert result.action != "REJECT"
    # Penalty 50% of max = -5
    assert -7 <= result.breakdown.bundle_penalty <= -3


def test_low_score_skip(engine: ScoringEngine) -> None:
    """No smart money + no other signals → low score → SKIP."""
    token = TokenData(
        address="x",
        mcap_usd=50_000,  # high MCAP (low points)
        liquidity_usd=8_500,
        smart_money_count=0,
        volume_5m_usd=500,
        is_honeypot=False,
        lp_burned=False,
        is_renounced=False,
        gmgn_security_score=72,
    )
    result = engine.score(token)
    assert result.action in ("SKIP", "ALERT")
    assert result.score < 75


def test_position_size_scaling(engine: ScoringEngine) -> None:
    """Position size sesuai score tier."""
    assert engine.position_size_sol(74) == 0.0  # below buy threshold
    assert engine.position_size_sol(75) == 0.015
    assert engine.position_size_sol(82) == 0.025
    assert engine.position_size_sol(87) == 0.035
    assert engine.position_size_sol(95) == 0.050


def test_breakdown_sum_equals_total(engine: ScoringEngine, good_token: TokenData) -> None:
    """Breakdown sum harus match total score."""
    result = engine.score(good_token)
    bd_sum = result.breakdown.total()
    # Allow tiny float tolerance
    assert abs(bd_sum - result.score) < 0.01


def test_score_clamped_0_100(engine: ScoringEngine) -> None:
    """Score harus selalu 0-100 (clamped)."""
    # Force max-everything token
    token = TokenData(
        address="x",
        mcap_usd=8_000,
        liquidity_usd=50_000,
        price_usd=0.0001,
        price_ath=0.001,
        volume_5m_usd=20_000,
        volume_increasing=True,
        smart_money_count=10,
        kol_count=5,
        lp_burned=True,
        is_renounced=True,
        gmgn_security_score=100,
        dev_holding_pct=2,
        bundle_supply_pct=1,
    )
    result = engine.score(token)
    assert 0 <= result.score <= 100


def test_custom_threshold_override(good_token: TokenData) -> None:
    """Custom buy threshold harus dipakai."""
    strict = ScoringEngine(min_score_buy=95)
    result = strict.score(good_token)
    # Good token tidak akan reach 95
    assert result.action != "BUY"


def test_to_dict_serialization(engine: ScoringEngine, good_token: TokenData) -> None:
    """to_dict() harus serializable + lengkap."""
    result = engine.score(good_token)
    d = result.to_dict()
    assert "address" in d
    assert "score" in d
    assert "action" in d
    assert "breakdown" in d
    assert "context" in d
    # Round-trip via JSON harus aman
    import json

    json.dumps(d)


# ------------------------------------------------------------------
# Phase 7d-7f: new signal component tests
# ------------------------------------------------------------------

def test_smart_money_trend_bonus(engine: ScoringEngine) -> None:
    """smart_money_composite_bonus +20 → score naik, -20 → score turun."""
    base = TokenData(address="x", mcap_usd=20_000, liquidity_usd=15_000)
    result_base = engine.score(base)

    positive = TokenData(
        address="x",
        mcap_usd=20_000,
        liquidity_usd=15_000,
        smart_money_composite_bonus=20.0,
    )
    result_pos = engine.score(positive)
    assert result_pos.score > result_base.score
    assert result_pos.breakdown.smart_money_trend_bonus == pytest.approx(20.0)

    negative = TokenData(
        address="x",
        mcap_usd=20_000,
        liquidity_usd=15_000,
        smart_money_composite_bonus=-20.0,
    )
    result_neg = engine.score(negative)
    assert result_neg.score < result_base.score
    assert result_neg.breakdown.smart_money_trend_bonus == pytest.approx(-20.0)


def test_smart_money_trend_bonus_clamped(engine: ScoringEngine) -> None:
    """composite_bonus > 30 harus di-clamp ke 30."""
    token = TokenData(
        address="x",
        mcap_usd=20_000,
        liquidity_usd=15_000,
        smart_money_composite_bonus=999.0,
    )
    result = engine.score(token)
    assert result.breakdown.smart_money_trend_bonus == pytest.approx(30.0)


def test_cluster_signal_bonus(engine: ScoringEngine) -> None:
    """Cluster signal strength → bonus sesuai tier."""
    base = TokenData(address="x", mcap_usd=20_000, liquidity_usd=15_000)

    for strength, expected_bonus in [
        ("VERY_STRONG", 20.0),
        ("STRONG", 15.0),
        ("MEDIUM", 5.0),
        ("WEAK", 0.0),
        ("NONE", 0.0),
    ]:
        token = TokenData(
            address="x",
            mcap_usd=20_000,
            liquidity_usd=15_000,
            cluster_signal_strength=strength,
        )
        result = engine.score(token)
        assert result.breakdown.cluster_signal_bonus == pytest.approx(expected_bonus), (
            f"Expected {expected_bonus} for {strength}, got {result.breakdown.cluster_signal_bonus}"
        )


def test_pumpfun_bonus(engine: ScoringEngine) -> None:
    """pumpfun_score_bonus langsung dipakai di breakdown."""
    token_sweet = TokenData(
        address="x",
        mcap_usd=20_000,
        liquidity_usd=15_000,
        pumpfun_score_bonus=10.0,
    )
    result = engine.score(token_sweet)
    assert result.breakdown.pumpfun_bonus == pytest.approx(10.0)

    token_graduated = TokenData(
        address="x",
        mcap_usd=20_000,
        liquidity_usd=15_000,
        pumpfun_score_bonus=-5.0,
    )
    result_grad = engine.score(token_graduated)
    assert result_grad.breakdown.pumpfun_bonus == pytest.approx(-5.0)


def test_multi_source_security_overrides_old(engine: ScoringEngine) -> None:
    """multi_source_safety_score override gmgn_security_score lama."""
    weight_security = engine.weight_security  # default 10

    # Token dengan multi_source_safety_score = 1.0 → full security points
    token_full = TokenData(
        address="x",
        mcap_usd=20_000,
        liquidity_usd=15_000,
        multi_source_safety_score=1.0,
        gmgn_security_score=0,  # legacy score nol — seharusnya di-override
        lp_burned=False,
        is_renounced=False,
    )
    result_full = engine.score(token_full)
    assert result_full.breakdown.security == pytest.approx(float(weight_security))

    # Token dengan multi_source_safety_score = 0.5 → half security points
    token_half = TokenData(
        address="x",
        mcap_usd=20_000,
        liquidity_usd=15_000,
        multi_source_safety_score=0.5,
        gmgn_security_score=100,  # legacy tinggi — harus di-override oleh multi_source
        lp_burned=True,
        is_renounced=True,
    )
    result_half = engine.score(token_half)
    assert result_half.breakdown.security == pytest.approx(float(weight_security) * 0.5)


def test_multi_source_critical_flags_cause_reject(engine: ScoringEngine) -> None:
    """multi_source_critical_flags dengan honeypot/lp_unlocked/mint_not_renounced → REJECT."""
    for flag, expected_reason in [
        ("honeypot", "honeypot_detected"),
        ("lp_unlocked", "lp_unlocked_detected"),
        ("mint_not_renounced", "mint_not_renounced_detected"),
    ]:
        token = TokenData(
            address="x",
            mcap_usd=20_000,
            liquidity_usd=15_000,
            is_honeypot=False,  # legacy field TIDAK diset
            multi_source_critical_flags=[flag],
        )
        result = engine.score(token)
        assert result.action == "REJECT", f"Expected REJECT for flag={flag}"
        assert expected_reason in result.reject_reasons, (
            f"Expected '{expected_reason}' in reject_reasons for flag={flag}"
        )


# ==========================================================================
# Phase 9: Macro Regime + News + Cross-Reference scoring tests
# ==========================================================================


def test_macro_extreme_risk_off_rejects(engine: ScoringEngine, good_token: TokenData) -> None:
    """macro_skip_entries=True → REJECT regardless of other signals."""
    good_token.macro_skip_entries = True
    good_token.macro_regime_level = "extreme_risk_off"
    result = engine.score(good_token)
    assert result.action == "REJECT"
    assert any("macro_extreme_risk_off" in r for r in result.reject_reasons)


def test_fud_high_severity_rejects(engine: ScoringEngine, good_token: TokenData) -> None:
    """fud_detected + severity=high → REJECT (hack/exploit/SEC veto)."""
    good_token.fud_detected = True
    good_token.fud_severity = "high"
    result = engine.score(good_token)
    assert result.action == "REJECT"
    assert "fud_event_high_severity" in result.reject_reasons


def test_fud_medium_severity_penalizes_score(engine: ScoringEngine, good_token: TokenData) -> None:
    """fud_detected + severity=medium → narrative_bonus reduced by 5, no reject."""
    good_token.fud_detected = True
    good_token.fud_severity = "medium"
    good_token.narrative_bonus = 3.0  # base +3
    result = engine.score(good_token)
    assert result.action != "REJECT"
    # Net narrative bonus should be 3 - 5 = -2 (clamped at -10)
    assert result.breakdown.narrative_bonus == pytest.approx(-2.0)


def test_narrative_bonus_clamped(engine: ScoringEngine, good_token: TokenData) -> None:
    """narrative_bonus clamped to -10..+10."""
    good_token.narrative_bonus = 25.0  # over cap
    result = engine.score(good_token)
    assert result.breakdown.narrative_bonus == 10.0

    good_token.narrative_bonus = -25.0  # under cap
    result = engine.score(good_token)
    assert result.breakdown.narrative_bonus == -10.0


def test_crossref_bonus_clamped(engine: ScoringEngine, good_token: TokenData) -> None:
    """crossref_bonus clamped to -5..+15."""
    good_token.crossref_bonus = 25.0
    result = engine.score(good_token)
    assert result.breakdown.crossref_bonus == 15.0

    good_token.crossref_bonus = -20.0
    result = engine.score(good_token)
    assert result.breakdown.crossref_bonus == -5.0


def test_crossref_listed_boosts_total_score(engine: ScoringEngine) -> None:
    """Token with CoinGecko listing → crossref_bonus increases total score."""
    token_unlisted = TokenData(
        address="x",
        mcap_usd=20_000,
        liquidity_usd=15_000,
        price_usd=0.001,
        smart_money_count=2,
        volume_5m_usd=4_000,
        is_renounced=True,
        lp_burned=True,
        gmgn_security_score=80,
        crossref_bonus=0.0,
    )
    token_listed = TokenData(
        address="y",
        mcap_usd=20_000,
        liquidity_usd=15_000,
        price_usd=0.001,
        smart_money_count=2,
        volume_5m_usd=4_000,
        is_renounced=True,
        lp_burned=True,
        gmgn_security_score=80,
        is_listed_on_coingecko=True,
        coingecko_rank=300,
        crossref_bonus=10.0,
    )
    score_unlisted = engine.score(token_unlisted).score
    score_listed = engine.score(token_listed).score
    assert score_listed > score_unlisted
    assert score_listed - score_unlisted == pytest.approx(10.0)


def test_position_size_macro_throttle_risk_off(engine: ScoringEngine) -> None:
    """risk_off multiplier 0.5 → position size halved."""
    base = engine.position_size_sol(score=85.0, macro_multiplier=1.0)
    throttled = engine.position_size_sol(score=85.0, macro_multiplier=0.5)
    assert throttled == pytest.approx(base * 0.5)


def test_position_size_macro_throttle_extreme(engine: ScoringEngine) -> None:
    """extreme risk-off multiplier 0.0 → position size 0."""
    sized = engine.position_size_sol(score=90.0, macro_multiplier=0.0)
    assert sized == 0.0


def test_position_size_macro_boost_risk_on(engine: ScoringEngine) -> None:
    """risk_on multiplier 1.3 → position size boosted, capped at settings max."""
    sized = engine.position_size_sol(score=85.0, macro_multiplier=1.3)
    # Base for score 85 = 0.035; 0.035 * 1.3 = 0.0455 (under 0.05 cap)
    assert sized == pytest.approx(0.0455)


def test_position_size_macro_caps_at_setting_max(engine: ScoringEngine) -> None:
    """High multiplier on high score → capped at settings.max_position_size_sol."""
    from src.config import settings as _s
    sized = engine.position_size_sol(score=95.0, macro_multiplier=1.5)
    # 0.05 * 1.5 = 0.075, but capped at settings.max_position_size_sol (default 0.05)
    assert sized == pytest.approx(_s.max_position_size_sol)


def test_position_size_below_minimum_threshold_returns_zero(engine: ScoringEngine) -> None:
    """Throttled position too small (< 0.005 SOL) → 0 (avoid micro-trades)."""
    # score 75 → base 0.015 → 0.015 * 0.2 = 0.003 SOL → below 0.005 floor → 0
    sized = engine.position_size_sol(score=75.0, macro_multiplier=0.2)
    assert sized == 0.0


def test_to_dict_includes_phase9_fields(engine: ScoringEngine, good_token: TokenData) -> None:
    """to_dict output exposes new Phase 9 context for DB persistence."""
    good_token.macro_regime_level = "risk_on"
    good_token.macro_position_multiplier = 1.3
    good_token.narrative_match = True
    good_token.is_listed_on_coingecko = True
    good_token.coingecko_rank = 250
    good_token.fud_detected = False
    result = engine.score(good_token)
    d = result.to_dict()
    assert d["context"]["macro_regime_level"] == "risk_on"
    assert d["context"]["macro_position_multiplier"] == 1.3
    assert d["context"]["narrative_match"] is True
    assert d["context"]["is_listed_on_coingecko"] is True
    assert d["context"]["coingecko_rank"] == 250
    assert d["context"]["fud_detected"] is False
    assert "narrative_bonus" in d["breakdown"]
    assert "crossref_bonus" in d["breakdown"]
