"""
Tests for MacroRegimeDetector — Phase 9 macro.

Coverage:
- EXTREME_RISK_OFF: BTC -10%+ crash → multiplier=0.0, skip_entries=True
- EXTREME_RISK_OFF: DXY surge +1.5% + SPX -2%+ → multiplier=0.0
- RISK_OFF: BTC -5%+ → multiplier=0.5
- RISK_OFF: SPX -1.5%+ → multiplier=0.5
- RISK_OFF: VIX spike +10%+ → multiplier=0.5
- NEUTRAL: default mixed signals → multiplier=1.0
- RISK_ON: BTC +5%+ with SPX flat/positive → multiplier=1.3
- RISK_ON: BTC +5%+ with weak DXY → multiplier=1.3
- Both clients None → NEUTRAL (graceful degrade)
- One client None → still classifies from available data
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.intel.macro_regime import MacroRegime, MacroRegimeDetector, RegimeLevel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_av_client(
    spx_change_pct: float = 0.0,
    dxy_change_pct: float = 0.0,
    vix_price: float = 10.0,
    vix_change_pct: float = 0.0,
    btc_24h_change_pct: float | None = None,
) -> AsyncMock:
    """
    Create a mocked AlphaVantageClient.

    If btc_24h_change_pct is provided, get_btc_daily returns data with two dates
    such that the pct change equals the given value.
    """
    client = AsyncMock()
    client.get_spx_quote = AsyncMock(
        return_value={"price": 450.0, "change_pct": spx_change_pct, "raw": {}}
    )
    client.get_dxy_quote = AsyncMock(
        return_value={"price": 29.0, "change_pct": dxy_change_pct, "raw": {}}
    )
    client.get_vix_quote = AsyncMock(
        return_value={"price": vix_price, "change_pct": vix_change_pct, "raw": {}}
    )

    # Build BTC daily that yields desired 24h change
    if btc_24h_change_pct is not None:
        prev = 40000.0
        latest = prev * (1 + btc_24h_change_pct / 100.0)
        btc_daily = {
            "Time Series (Digital Currency Daily)": {
                "2024-01-02": {"4a. close (USD)": str(latest)},
                "2024-01-01": {"4a. close (USD)": str(prev)},
            }
        }
    else:
        btc_daily = {}

    client.get_btc_daily = AsyncMock(return_value=btc_daily)
    return client


def _make_cq_client(
    mvrv: float = 2.0,
    funding_rate: float = 0.0001,
) -> AsyncMock:
    """Create a mocked CryptoQuantClient."""
    client = AsyncMock()
    client.get_btc_mvrv_ratio = AsyncMock(
        return_value={"data": [{"mvrv_ratio": mvrv}], "status": "ok"}
    )
    client.get_btc_funding_rates = AsyncMock(
        return_value={"data": [{"funding_rate": funding_rate}], "status": "ok"}
    )
    return client


async def _detect(av=None, cq=None) -> MacroRegime:
    """Helper: create detector and run detect_regime bypassing cache."""
    detector = MacroRegimeDetector(cryptoquant=cq, alphavantage=av)
    # Call the underlying method directly, bypassing the @cached decorator
    return await detector.detect_regime.__wrapped__(detector)


# ---------------------------------------------------------------------------
# EXTREME_RISK_OFF tests
# ---------------------------------------------------------------------------

class TestExtremeRiskOff:

    @pytest.mark.asyncio
    async def test_btc_crash_minus_10pct(self):
        """BTC -10% in 24h → EXTREME_RISK_OFF, multiplier=0.0, skip_entries=True."""
        av = _make_av_client(btc_24h_change_pct=-10.5, spx_change_pct=-0.5)
        cq = _make_cq_client()
        regime = await _detect(av=av, cq=cq)

        assert regime.level == RegimeLevel.EXTREME_RISK_OFF
        assert regime.position_size_multiplier == pytest.approx(0.0)
        assert regime.should_skip_entries is True
        assert len(regime.reasons) > 0

    @pytest.mark.asyncio
    async def test_dxy_surge_and_spx_crash(self):
        """DXY +1.5% AND SPX -2% → EXTREME_RISK_OFF."""
        av = _make_av_client(
            btc_24h_change_pct=-3.0,  # not bad enough alone
            spx_change_pct=-2.5,
            dxy_change_pct=1.8,
        )
        cq = _make_cq_client()
        regime = await _detect(av=av, cq=cq)

        assert regime.level == RegimeLevel.EXTREME_RISK_OFF
        assert regime.should_skip_entries is True

    @pytest.mark.asyncio
    async def test_btc_exactly_minus_10_is_extreme(self):
        """BTC exactly -10.0% triggers EXTREME_RISK_OFF (boundary)."""
        av = _make_av_client(btc_24h_change_pct=-10.0)
        cq = _make_cq_client()
        regime = await _detect(av=av, cq=cq)

        assert regime.level == RegimeLevel.EXTREME_RISK_OFF


# ---------------------------------------------------------------------------
# RISK_OFF tests
# ---------------------------------------------------------------------------

class TestRiskOff:

    @pytest.mark.asyncio
    async def test_btc_minus_5pct(self):
        """BTC -5% in 24h (but not -10%) → RISK_OFF, multiplier=0.5."""
        av = _make_av_client(btc_24h_change_pct=-6.0, spx_change_pct=-0.5)
        cq = _make_cq_client()
        regime = await _detect(av=av, cq=cq)

        assert regime.level == RegimeLevel.RISK_OFF
        assert regime.position_size_multiplier == pytest.approx(0.5)
        assert regime.should_skip_entries is False

    @pytest.mark.asyncio
    async def test_spx_minus_1_5pct(self):
        """SPX -1.5%+ → RISK_OFF even if BTC is flat."""
        av = _make_av_client(btc_24h_change_pct=0.5, spx_change_pct=-2.0)
        cq = _make_cq_client()
        regime = await _detect(av=av, cq=cq)

        assert regime.level == RegimeLevel.RISK_OFF
        assert regime.position_size_multiplier == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_vix_spike_10pct(self):
        """VIX (VIXY) daily spike +10%+ → RISK_OFF."""
        av = _make_av_client(
            btc_24h_change_pct=-1.0,
            spx_change_pct=-0.5,
            vix_price=15.0,
            vix_change_pct=12.0,
        )
        cq = _make_cq_client()
        regime = await _detect(av=av, cq=cq)

        assert regime.level == RegimeLevel.RISK_OFF


# ---------------------------------------------------------------------------
# NEUTRAL tests
# ---------------------------------------------------------------------------

class TestNeutral:

    @pytest.mark.asyncio
    async def test_mixed_signals_neutral(self):
        """Mixed / mild signals → NEUTRAL, multiplier=1.0."""
        av = _make_av_client(
            btc_24h_change_pct=1.0,
            spx_change_pct=0.3,
            dxy_change_pct=-0.1,
            vix_price=12.0,
            vix_change_pct=2.0,
        )
        cq = _make_cq_client()
        regime = await _detect(av=av, cq=cq)

        assert regime.level == RegimeLevel.NEUTRAL
        assert regime.position_size_multiplier == pytest.approx(1.0)
        assert regime.should_skip_entries is False

    @pytest.mark.asyncio
    async def test_both_clients_none_returns_neutral(self):
        """No clients available → NEUTRAL by default."""
        regime = await _detect(av=None, cq=None)

        assert regime.level == RegimeLevel.NEUTRAL
        assert regime.position_size_multiplier == pytest.approx(1.0)
        assert regime.should_skip_entries is False

    @pytest.mark.asyncio
    async def test_cq_client_none_still_classifies(self):
        """CryptoQuant None, AV present → still classifies from AV data."""
        av = _make_av_client(btc_24h_change_pct=1.5, spx_change_pct=0.2)
        regime = await _detect(av=av, cq=None)

        assert regime.level == RegimeLevel.NEUTRAL
        assert regime.mvrv_ratio == pytest.approx(0.0)  # no CQ data


# ---------------------------------------------------------------------------
# RISK_ON tests
# ---------------------------------------------------------------------------

class TestRiskOn:

    @pytest.mark.asyncio
    async def test_btc_plus_5pct_with_spx_flat(self):
        """BTC +5%+ AND SPX flat/positive → RISK_ON, multiplier=1.3."""
        av = _make_av_client(
            btc_24h_change_pct=7.0,
            spx_change_pct=0.5,
            dxy_change_pct=0.2,  # slight DXY up, but SPX ok
        )
        cq = _make_cq_client()
        regime = await _detect(av=av, cq=cq)

        assert regime.level == RegimeLevel.RISK_ON
        assert regime.position_size_multiplier == pytest.approx(1.3)
        assert regime.should_skip_entries is False

    @pytest.mark.asyncio
    async def test_btc_plus_5pct_with_weak_dxy(self):
        """BTC +5%+ AND DXY weak (falling) → RISK_ON even if SPX slightly down."""
        av = _make_av_client(
            btc_24h_change_pct=6.5,
            spx_change_pct=-0.3,  # SPX slightly negative but not RISK_OFF threshold
            dxy_change_pct=-0.5,  # DXY weak
        )
        cq = _make_cq_client()
        regime = await _detect(av=av, cq=cq)

        assert regime.level == RegimeLevel.RISK_ON

    @pytest.mark.asyncio
    async def test_btc_4_9pct_not_risk_on(self):
        """BTC +4.9% (just under threshold) → NEUTRAL, not RISK_ON."""
        av = _make_av_client(btc_24h_change_pct=4.9, spx_change_pct=0.5)
        cq = _make_cq_client()
        regime = await _detect(av=av, cq=cq)

        assert regime.level == RegimeLevel.NEUTRAL


# ---------------------------------------------------------------------------
# Regime data population tests
# ---------------------------------------------------------------------------

class TestRegimeDataFields:

    @pytest.mark.asyncio
    async def test_regime_fields_populated(self):
        """MacroRegime dataclass fields are populated from API responses."""
        av = _make_av_client(
            btc_24h_change_pct=2.0,
            spx_change_pct=0.5,
            dxy_change_pct=-0.3,
            vix_price=13.5,
            vix_change_pct=1.0,
        )
        cq = _make_cq_client(mvrv=2.8, funding_rate=0.0002)
        regime = await _detect(av=av, cq=cq)

        assert regime.spx_change_pct == pytest.approx(0.5)
        assert regime.dxy_change_pct == pytest.approx(-0.3)
        assert regime.vix_value == pytest.approx(13.5)
        assert regime.mvrv_ratio == pytest.approx(2.8)
        assert regime.funding_rate_avg == pytest.approx(0.0002)
        assert isinstance(regime.reasons, list)
        assert len(regime.reasons) > 0

    @pytest.mark.asyncio
    async def test_multiplier_capped_at_zero(self):
        """Extreme risk-off multiplier is exactly 0.0 (not negative)."""
        av = _make_av_client(btc_24h_change_pct=-15.0)
        cq = _make_cq_client()
        regime = await _detect(av=av, cq=cq)

        assert regime.position_size_multiplier >= 0.0
        assert regime.position_size_multiplier == pytest.approx(0.0)
