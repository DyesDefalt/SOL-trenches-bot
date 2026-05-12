"""
Tests untuk SmartMoneyAggregator + SmartMoneySignal.

Coverage:
- Composite score logic untuk berbagai kombinasi Nansen trend + GMGN cluster
- Verdict thresholds (semua 5 kategori)
- Graceful handling saat Nansen tidak return data (token tidak ditrack)
- Graceful handling saat GMGN error
- FOMO warning (KOL without smart money)
- Max bonus cap (cluster + sustained_accumulation)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.intel.smart_money import (
    SmartMoneyAggregator,
    SmartMoneySignal,
    _compute_composite_score,
    _compute_verdict,
)

TOKEN = "TokenABC123"
CHAIN = "sol"


# ---------------------------------------------------------------------------
# Unit tests: _compute_composite_score
# ---------------------------------------------------------------------------

class TestCompositeScoreLogic:
    """Test scoring rules secara unit tanpa network calls."""

    def _make_signal(self, **kwargs) -> SmartMoneySignal:
        return SmartMoneySignal(token_address=TOKEN, chain=CHAIN, **kwargs)

    def test_sustained_accumulation_base_score(self):
        """sustained_accumulation tanpa cluster = +20."""
        signal = self._make_signal(
            nansen_trend="sustained_accumulation",
            gmgn_smart_money_count_15m=0,
        )
        assert _compute_composite_score(signal) == 20.0

    def test_fresh_entry_with_smart_trader_confirmation(self):
        """fresh_entry + smart_trader_flow positif = +15."""
        signal = self._make_signal(
            nansen_trend="fresh_entry",
            nansen_smart_trader_flow=5000.0,
            gmgn_smart_money_count_15m=0,
        )
        assert _compute_composite_score(signal) == 15.0

    def test_fresh_entry_without_smart_trader_confirmation(self):
        """fresh_entry tanpa smart_trader backing = +5 saja."""
        signal = self._make_signal(
            nansen_trend="fresh_entry",
            nansen_smart_trader_flow=0.0,
            gmgn_smart_money_count_15m=0,
        )
        assert _compute_composite_score(signal) == 5.0

    def test_reducing_trend(self):
        """reducing = -10."""
        signal = self._make_signal(
            nansen_trend="reducing",
            gmgn_smart_money_count_15m=0,
        )
        assert _compute_composite_score(signal) == -10.0

    def test_distribution_trend(self):
        """distribution = -25."""
        signal = self._make_signal(
            nansen_trend="distribution",
            gmgn_smart_money_count_15m=0,
        )
        assert _compute_composite_score(signal) == -25.0

    def test_gmgn_cluster_additive(self):
        """GMGN cluster (3+ wallets 15m) = +15 additive ke Nansen score."""
        signal = self._make_signal(
            nansen_trend="fresh_entry",
            nansen_smart_trader_flow=0.0,
            gmgn_smart_money_count_15m=4,
        )
        # fresh_entry without smart_trader = +5, cluster = +15 additive → +20
        assert _compute_composite_score(signal) == 20.0

    def test_cluster_plus_sustained_accumulation_capped_at_30(self):
        """cluster + sustained_accumulation = max bonus +30 (capped)."""
        signal = self._make_signal(
            nansen_trend="sustained_accumulation",
            gmgn_smart_money_count_15m=5,
        )
        score = _compute_composite_score(signal)
        assert score == 30.0

    def test_kol_without_smart_money_fomo_penalty(self):
        """KOL > 2 tapi 0 smart money = -5 FOMO penalty."""
        signal = self._make_signal(
            nansen_trend="unknown",
            gmgn_kol_count_15m=3,
            gmgn_smart_money_count_15m=0,
        )
        assert _compute_composite_score(signal) == -5.0

    def test_score_bounded_below_minus_30(self):
        """Score tidak bisa di bawah -30."""
        signal = self._make_signal(
            nansen_trend="distribution",  # -25
            gmgn_kol_count_15m=5,       # -5 FOMO → total would be -30, capped
            gmgn_smart_money_count_15m=0,
        )
        score = _compute_composite_score(signal)
        assert score >= -30.0


# ---------------------------------------------------------------------------
# Unit tests: _compute_verdict
# ---------------------------------------------------------------------------

class TestVerdictThresholds:
    """Test bahwa threshold verdict mapping benar sesuai spec."""

    def test_strong_buy_at_25(self):
        assert _compute_verdict(25.0) == "STRONG_BUY"

    def test_strong_buy_above_25(self):
        assert _compute_verdict(30.0) == "STRONG_BUY"

    def test_buy_at_10(self):
        assert _compute_verdict(10.0) == "BUY"

    def test_buy_at_24(self):
        assert _compute_verdict(24.9) == "BUY"

    def test_neutral_at_0(self):
        assert _compute_verdict(0.0) == "NEUTRAL"

    def test_neutral_at_minus_5(self):
        assert _compute_verdict(-5.0) == "NEUTRAL"

    def test_avoid_at_minus_6(self):
        assert _compute_verdict(-6.0) == "AVOID"

    def test_avoid_at_minus_15(self):
        assert _compute_verdict(-15.0) == "AVOID"

    def test_strong_avoid_below_minus_15(self):
        assert _compute_verdict(-16.0) == "STRONG_AVOID"

    def test_strong_avoid_at_minus_30(self):
        assert _compute_verdict(-30.0) == "STRONG_AVOID"


# ---------------------------------------------------------------------------
# Integration tests: SmartMoneyAggregator.get_signal (mocked I/O)
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_nansen():
    """NansenClient mock dengan data default (token ditrack)."""
    client = AsyncMock()
    client.get_flow_intelligence.return_value = {
        "netflow_1h": 1000.0,
        "netflow_24h": 5000.0,
        "netflow_7d": 15000.0,
        "netflow_30d": 40000.0,
        "trend": "sustained_accumulation",
    }
    client.get_smart_money_dex_trades_for_token.return_value = [
        {"label": "smart_trader", "usd_value": 3000.0, "side": "buy"},
        {"label": "fund", "usd_value": 10000.0, "side": "buy"},
    ]
    return client


@pytest.fixture
def mock_gmgn_with_cluster():
    """GMGNClient mock dengan 3+ smart money buyers untuk token target."""
    import time
    now = int(time.time())
    client = AsyncMock()
    client.get_smart_money_trades.return_value = [
        {"base_address": TOKEN, "wallet": "wallet1", "timestamp": now - 60, "usd_value": 500.0},
        {"base_address": TOKEN, "wallet": "wallet2", "timestamp": now - 120, "usd_value": 600.0},
        {"base_address": TOKEN, "wallet": "wallet3", "timestamp": now - 180, "usd_value": 400.0},
        {"base_address": "OtherToken", "wallet": "wallet4", "timestamp": now - 60, "usd_value": 100.0},
    ]
    client.get_kol_trades.return_value = []
    return client


@pytest.fixture
def mock_gmgn_no_cluster():
    """GMGNClient mock tanpa smart money activity untuk token target."""
    client = AsyncMock()
    client.get_smart_money_trades.return_value = []
    client.get_kol_trades.return_value = []
    return client


@pytest.fixture
def mock_registry():
    return MagicMock()


class TestSmartMoneyAggregator:
    """Integration tests SmartMoneyAggregator dengan mocked clients."""

    @pytest.mark.asyncio
    async def test_strong_buy_when_nansen_accumulation_and_cluster(
        self, mock_nansen, mock_gmgn_with_cluster, mock_registry
    ):
        """sustained_accumulation + cluster → STRONG_BUY dengan score 30."""
        agg = SmartMoneyAggregator(mock_nansen, mock_gmgn_with_cluster, mock_registry)
        signal = await agg.get_signal(TOKEN)

        assert signal.verdict == "STRONG_BUY"
        assert signal.composite_score_bonus == 30.0
        assert signal.nansen_available is True
        assert signal.gmgn_smart_money_count_15m == 3

    @pytest.mark.asyncio
    async def test_nansen_unavailable_falls_back_to_gmgn_only(
        self, mock_gmgn_with_cluster, mock_registry
    ):
        """Jika Nansen tidak return data, fallback ke GMGN-only signal."""
        nansen = AsyncMock()
        nansen.get_flow_intelligence.return_value = {}  # empty = no data
        nansen.get_smart_money_dex_trades_for_token.return_value = []

        agg = SmartMoneyAggregator(nansen, mock_gmgn_with_cluster, mock_registry)
        signal = await agg.get_signal(TOKEN)

        # Nansen tidak available → trend=unknown (0 score dari Nansen)
        assert signal.nansen_available is False
        assert signal.nansen_trend == "unknown"
        # GMGN cluster masih berkontribusi +15
        assert signal.composite_score_bonus == 15.0
        assert signal.verdict == "BUY"

    @pytest.mark.asyncio
    async def test_nansen_exception_gracefully_handled(
        self, mock_gmgn_with_cluster, mock_registry
    ):
        """Exception dari Nansen tidak crash aggregator."""
        nansen = AsyncMock()
        nansen.get_flow_intelligence.side_effect = Exception("nansen_timeout")

        agg = SmartMoneyAggregator(nansen, mock_gmgn_with_cluster, mock_registry)
        # Should not raise
        signal = await agg.get_signal(TOKEN)

        assert signal.token_address == TOKEN
        assert signal.nansen_available is False

    @pytest.mark.asyncio
    async def test_gmgn_exception_gracefully_handled(
        self, mock_nansen, mock_registry
    ):
        """Exception dari GMGN tidak crash aggregator."""
        gmgn = AsyncMock()
        gmgn.get_smart_money_trades.side_effect = Exception("gmgn_connection_error")
        gmgn.get_kol_trades.side_effect = Exception("gmgn_connection_error")

        agg = SmartMoneyAggregator(mock_nansen, gmgn, mock_registry)
        signal = await agg.get_signal(TOKEN)

        # Nansen masih jalan → sustained_accumulation = +20
        assert signal.token_address == TOKEN
        assert signal.composite_score_bonus == 20.0

    @pytest.mark.asyncio
    async def test_distribution_trend_with_no_cluster(
        self, mock_gmgn_no_cluster, mock_registry
    ):
        """distribution trend tanpa GMGN counter → STRONG_AVOID."""
        nansen = AsyncMock()
        nansen.get_flow_intelligence.return_value = {
            "netflow_24h": -50000.0,
            "trend": "distribution",
        }
        nansen.get_smart_money_dex_trades_for_token.return_value = []

        agg = SmartMoneyAggregator(nansen, mock_gmgn_no_cluster, mock_registry)
        signal = await agg.get_signal(TOKEN)

        assert signal.verdict == "STRONG_AVOID"
        assert signal.composite_score_bonus == -25.0

    @pytest.mark.asyncio
    async def test_both_sources_unavailable_returns_neutral(self, mock_registry):
        """Kedua sumber gagal → NEUTRAL dengan score 0."""
        nansen = AsyncMock()
        nansen.get_flow_intelligence.side_effect = Exception("nansen_down")

        gmgn = AsyncMock()
        gmgn.get_smart_money_trades.side_effect = Exception("gmgn_down")
        gmgn.get_kol_trades.side_effect = Exception("gmgn_down")

        agg = SmartMoneyAggregator(nansen, gmgn, mock_registry)
        signal = await agg.get_signal(TOKEN)

        assert signal.composite_score_bonus == 0.0
        assert signal.verdict == "NEUTRAL"

    @pytest.mark.asyncio
    async def test_kol_fomo_warning_applied(self, mock_nansen, mock_registry):
        """KOL > 2 tapi 0 smart money → -5 dari GMGN, mereduksi Nansen score."""
        import time
        now = int(time.time())

        gmgn = AsyncMock()
        gmgn.get_smart_money_trades.return_value = []  # tidak ada smart money beli token ini
        gmgn.get_kol_trades.return_value = [
            {"base_address": TOKEN, "wallet": "kol1", "timestamp": now - 100},
            {"base_address": TOKEN, "wallet": "kol2", "timestamp": now - 200},
            {"base_address": TOKEN, "wallet": "kol3", "timestamp": now - 300},
        ]

        # Override nansen ke "mixed" (netral, 0) supaya FOMO visible
        nansen = AsyncMock()
        nansen.get_flow_intelligence.return_value = {"trend": "mixed"}
        nansen.get_smart_money_dex_trades_for_token.return_value = []

        agg = SmartMoneyAggregator(nansen, gmgn, mock_registry)
        signal = await agg.get_signal(TOKEN)

        assert signal.gmgn_kol_count_15m == 3
        assert signal.gmgn_smart_money_count_15m == 0
        assert signal.composite_score_bonus == -5.0
        assert signal.verdict == "NEUTRAL"  # -5 masih di range NEUTRAL (-5 cutoff)
