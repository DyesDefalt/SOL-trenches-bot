"""
Tests untuk ClusterDetector + ClusterSignal + signal_strength_from_count.

Coverage:
- Deteksi cluster dari 5+ trades wallet unik yang beli token sama
- Sort by strength desc, lalu total_usd desc
- Tidak return cluster saat wallet_count < 3
- signal_strength_from_count untuk semua kombinasi
- Deduplication wallet address yang sama beli berkali-kali
- get_cluster_for_token: return None jika < 3 wallets
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from src.intel.cluster_detector import ClusterDetector, ClusterSignal, signal_strength_from_count

NOW = int(time.time())
TOKEN_A = "TokenAAAAA"
TOKEN_B = "TokenBBBBB"
TOKEN_C = "TokenCCCCC"


def _make_trade(
    token: str,
    wallet: str,
    ts: int,
    usd: float = 500.0,
    side: str = "buy",
) -> dict:
    return {
        "base_address": token,
        "wallet": wallet,
        "timestamp": ts,
        "usd_value": usd,
        "side": side,
    }


# ---------------------------------------------------------------------------
# Unit tests: signal_strength_from_count
# ---------------------------------------------------------------------------

class TestSignalStrength:
    """Test strength calculation function secara unit."""

    def test_single_wallet_is_weak(self):
        assert signal_strength_from_count(1, False, 30) == "WEAK"

    def test_two_wallets_is_medium(self):
        assert signal_strength_from_count(2, False, 30) == "MEDIUM"

    def test_three_wallets_no_kol_is_strong(self):
        assert signal_strength_from_count(3, False, 30) == "STRONG"

    def test_five_wallets_no_kol_is_strong(self):
        assert signal_strength_from_count(5, False, 30) == "STRONG"

    def test_three_wallets_kol_within_15min_is_very_strong(self):
        """3+ wallets dalam 15 menit dengan KOL participation = VERY_STRONG."""
        assert signal_strength_from_count(3, True, 15) == "VERY_STRONG"

    def test_three_wallets_kol_beyond_15min_is_strong(self):
        """KOL participation tapi window > 15 menit = STRONG bukan VERY_STRONG."""
        assert signal_strength_from_count(3, True, 16) == "STRONG"

    def test_zero_wallets_is_weak(self):
        assert signal_strength_from_count(0, False, 30) == "WEAK"


# ---------------------------------------------------------------------------
# Integration tests: ClusterDetector.detect_clusters
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_gmgn_multi_token():
    """
    GMGN mock dengan cluster di TOKEN_A (5 wallets) dan TOKEN_B (4 wallets).
    TOKEN_C hanya 2 wallets → tidak masuk cluster.
    """
    client = AsyncMock()
    client.get_smart_money_trades.return_value = [
        # TOKEN_A — 5 unique wallets, STRONG cluster
        _make_trade(TOKEN_A, "wallet_a1", NOW - 100, usd=1000.0),
        _make_trade(TOKEN_A, "wallet_a2", NOW - 200, usd=800.0),
        _make_trade(TOKEN_A, "wallet_a3", NOW - 300, usd=900.0),
        _make_trade(TOKEN_A, "wallet_a4", NOW - 400, usd=700.0),
        _make_trade(TOKEN_A, "wallet_a5", NOW - 500, usd=600.0),
        # TOKEN_B — 4 unique wallets, smaller total_usd
        _make_trade(TOKEN_B, "wallet_b1", NOW - 150, usd=200.0),
        _make_trade(TOKEN_B, "wallet_b2", NOW - 250, usd=150.0),
        _make_trade(TOKEN_B, "wallet_b3", NOW - 350, usd=180.0),
        _make_trade(TOKEN_B, "wallet_b4", NOW - 450, usd=120.0),
        # TOKEN_C — hanya 2 wallets, tidak memenuhi threshold
        _make_trade(TOKEN_C, "wallet_c1", NOW - 100, usd=300.0),
        _make_trade(TOKEN_C, "wallet_c2", NOW - 200, usd=400.0),
        # Duplicate wallet untuk TOKEN_A — harus didedup
        _make_trade(TOKEN_A, "wallet_a1", NOW - 50, usd=500.0),
    ]
    client.get_kol_trades.return_value = []
    return client


class TestClusterDetector:
    """Integration tests ClusterDetector dengan mocked GMGN."""

    @pytest.mark.asyncio
    async def test_cluster_detected_for_5_wallets(self, mock_gmgn_multi_token):
        """TOKEN_A dengan 5 unique wallets harus terdeteksi sebagai cluster."""
        detector = ClusterDetector(mock_gmgn_multi_token)
        clusters = await detector.detect_clusters(chain="sol", limit=200, window_minutes=30)

        token_a_clusters = [c for c in clusters if c.token_address == TOKEN_A.lower()]
        assert len(token_a_clusters) == 1
        cluster = token_a_clusters[0]
        assert cluster.wallet_count == 5  # wallet_a1 harus didedup
        assert cluster.strength == "STRONG"

    @pytest.mark.asyncio
    async def test_token_with_less_than_3_wallets_excluded(self, mock_gmgn_multi_token):
        """TOKEN_C dengan hanya 2 wallets tidak boleh muncul di hasil."""
        detector = ClusterDetector(mock_gmgn_multi_token)
        clusters = await detector.detect_clusters(chain="sol", limit=200, window_minutes=30)

        token_c_found = any(c.token_address == TOKEN_C.lower() for c in clusters)
        assert not token_c_found

    @pytest.mark.asyncio
    async def test_sort_by_total_usd_within_same_strength(self, mock_gmgn_multi_token):
        """TOKEN_A (total $4000) harus muncul sebelum TOKEN_B (total $650) — same STRONG."""
        detector = ClusterDetector(mock_gmgn_multi_token)
        clusters = await detector.detect_clusters(chain="sol", limit=200, window_minutes=30)

        strong_clusters = [c for c in clusters if c.strength == "STRONG"]
        # TOKEN_A punya total_usd lebih tinggi → harus duluan
        assert strong_clusters[0].token_address == TOKEN_A.lower()
        assert strong_clusters[1].token_address == TOKEN_B.lower()

    @pytest.mark.asyncio
    async def test_very_strong_when_kol_participates(self):
        """KOL beli TOKEN_A dalam window 15 menit → VERY_STRONG."""
        gmgn = AsyncMock()
        gmgn.get_smart_money_trades.return_value = [
            _make_trade(TOKEN_A, "wallet1", NOW - 100),
            _make_trade(TOKEN_A, "wallet2", NOW - 200),
            _make_trade(TOKEN_A, "wallet3", NOW - 300),
        ]
        gmgn.get_kol_trades.return_value = [
            # KOL beli TOKEN_A dalam window
            _make_trade(TOKEN_A, "kol_wallet1", NOW - 150),
        ]

        detector = ClusterDetector(gmgn)
        clusters = await detector.detect_clusters(chain="sol", limit=200, window_minutes=15)

        assert len(clusters) == 1
        assert clusters[0].kol_participation is True
        assert clusters[0].strength == "VERY_STRONG"

    @pytest.mark.asyncio
    async def test_wallet_deduplication(self):
        """Wallet yang beli dua kali hanya dihitung sekali."""
        gmgn = AsyncMock()
        gmgn.get_smart_money_trades.return_value = [
            _make_trade(TOKEN_A, "wallet1", NOW - 100),
            _make_trade(TOKEN_A, "wallet1", NOW - 200),  # duplicate
            _make_trade(TOKEN_A, "wallet1", NOW - 300),  # duplicate
            _make_trade(TOKEN_A, "wallet2", NOW - 400),
            _make_trade(TOKEN_A, "wallet3", NOW - 500),
        ]
        gmgn.get_kol_trades.return_value = []

        detector = ClusterDetector(gmgn)
        clusters = await detector.detect_clusters()

        assert clusters[0].wallet_count == 3  # wallet1 didedup ke 1

    @pytest.mark.asyncio
    async def test_get_cluster_for_token_returns_none_when_no_cluster(self):
        """get_cluster_for_token harus return None jika < 3 unique wallets."""
        gmgn = AsyncMock()
        gmgn.get_smart_money_trades.return_value = [
            _make_trade(TOKEN_A, "wallet1", NOW - 100),
            _make_trade(TOKEN_A, "wallet2", NOW - 200),
        ]
        gmgn.get_kol_trades.return_value = []

        detector = ClusterDetector(gmgn)
        result = await detector.get_cluster_for_token(TOKEN_A)

        assert result is None

    @pytest.mark.asyncio
    async def test_get_cluster_for_token_returns_signal_when_cluster_exists(self):
        """get_cluster_for_token return ClusterSignal jika 3+ wallets."""
        gmgn = AsyncMock()
        gmgn.get_smart_money_trades.return_value = [
            _make_trade(TOKEN_A, "wallet1", NOW - 100, usd=300.0),
            _make_trade(TOKEN_A, "wallet2", NOW - 200, usd=400.0),
            _make_trade(TOKEN_A, "wallet3", NOW - 300, usd=500.0),
            # Token lain harus diabaikan
            _make_trade(TOKEN_B, "wallet_other", NOW - 100),
        ]
        gmgn.get_kol_trades.return_value = []

        detector = ClusterDetector(gmgn)
        result = await detector.get_cluster_for_token(TOKEN_A)

        assert result is not None
        assert result.token_address == TOKEN_A
        assert result.wallet_count == 3
        assert result.total_usd == pytest.approx(1200.0)
        assert result.strength == "STRONG"
