"""
Cluster Detector — deteksi 3+ smart money wallet beli token yang sama dalam window waktu.

Cluster signal adalah sinyal paling kuat dalam sistem: ketika beberapa smart wallet
independently memilih token yang sama dalam window pendek, probabilitas bahwa ada
alpha nyata jauh lebih tinggi.

# Definisi Cluster:
- WEAK: 1 wallet (tidak ada cluster)
- MEDIUM: 2-3 wallets
- STRONG: 3+ wallets dalam 30 menit, smart money only
- VERY_STRONG: 3+ wallets dalam 15 menit + ada partisipasi KOL

# Flow:
1. Fetch recent smart money trades dari GMGN (buy side)
2. Group by token (base_address)
3. Untuk tiap token: count unique wallets dalam time window
4. Return list ClusterSignal dimana wallet_count >= 3
5. Optional: cross-reference dengan Nansen untuk konfirmasi institutional backing

# Sort output: VERY_STRONG → STRONG → MEDIUM → WEAK, lalu total_usd desc.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from src.infra.logger import get_logger

if TYPE_CHECKING:
    from src.clients.gmgn import GMGNClient
    from src.intel.nansen_client import NansenClient

log = get_logger(__name__)

ClusterStrength = Literal["WEAK", "MEDIUM", "STRONG", "VERY_STRONG"]

# Urutan strength untuk sort (tinggi = lebih kuat)
_STRENGTH_ORDER: dict[ClusterStrength, int] = {
    "VERY_STRONG": 3,
    "STRONG": 2,
    "MEDIUM": 1,
    "WEAK": 0,
}


@dataclass
class ClusterSignal:
    """
    Sinyal cluster smart money untuk satu token.

    wallet_count >= 3 = cluster yang layak diperhatikan.
    Strength ditentukan dari kombinasi jumlah wallet, window waktu, dan KOL participation.
    """

    token_address: str
    chain: str
    wallet_count: int  # >= 3 = real cluster
    total_usd: float  # Total USD value semua trades di cluster
    earliest_buy_ts: int  # Unix timestamp
    latest_buy_ts: int  # Unix timestamp
    time_window_seconds: int  # latest - earliest
    wallet_addresses: list[str]  # unique wallet addresses
    strength: ClusterStrength
    kol_participation: bool = False  # Ada KOL di antara buyers?


def signal_strength_from_count(
    wallet_count: int,
    kol_participation: bool,
    window_minutes: int,
) -> ClusterStrength:
    """
    Tentukan strength cluster berdasarkan jumlah wallet, KOL participation, dan window.

    Args:
        wallet_count: Jumlah unique smart wallet yang beli token ini.
        kol_participation: True jika ada KOL ikut beli dalam window yang sama.
        window_minutes: Time window yang dipakai untuk deteksi (biasanya 15 atau 30 menit).

    Returns:
        WEAK     — 1 wallet
        MEDIUM   — 2-3 wallets
        STRONG   — 3+ wallets dalam window (standard)
        VERY_STRONG — 3+ wallets dalam 15 menit + ada KOL
    """
    if wallet_count <= 1:
        return "WEAK"
    if wallet_count <= 2:
        return "MEDIUM"
    # wallet_count >= 3
    if kol_participation and window_minutes <= 15:
        return "VERY_STRONG"
    return "STRONG"


class ClusterDetector:
    """
    Deteksi cluster sinyal dari recent smart money trades.

    Usage:
        detector = ClusterDetector(gmgn_client, nansen_client=nansen_client)
        clusters = await detector.detect_clusters(chain="sol", limit=200, window_minutes=30)
        # Filter hanya STRONG/VERY_STRONG
        hot = [c for c in clusters if c.strength in ("STRONG", "VERY_STRONG")]

    Tanpa Nansen:
        detector = ClusterDetector(gmgn_client)  # nansen opsional
        clusters = await detector.detect_clusters()
    """

    def __init__(
        self,
        gmgn_client: "GMGNClient",
        nansen_client: "NansenClient | None" = None,
    ) -> None:
        self._gmgn = gmgn_client
        self._nansen = nansen_client  # opsional untuk cross-reference

    async def detect_clusters(
        self,
        chain: str = "sol",
        limit: int = 200,
        window_minutes: int = 30,
    ) -> list[ClusterSignal]:
        """
        Scan recent smart money trades untuk cluster signals.

        Fetches GMGN smart money + KOL trades secara paralel, group by token,
        count unique wallets dalam time window, return clusters dimana wallet_count >= 3.

        Args:
            chain: Chain target ("sol", "bsc", "base", dll.)
            limit: Max jumlah trades per fetch dari GMGN (max 200).
            window_minutes: Window waktu untuk cluster detection.

        Returns:
            List ClusterSignal terurut: strength desc, lalu total_usd desc.
        """
        log.info(
            "cluster_detect_start",
            chain=chain,
            limit=limit,
            window_minutes=window_minutes,
        )

        # Fetch smart money + KOL secara paralel
        sm_trades, kol_trades = await asyncio.gather(
            self._gmgn.get_smart_money_trades(chain=chain, limit=limit, side="buy"),
            self._gmgn.get_kol_trades(chain=chain, limit=limit, side="buy"),
        )

        cutoff_ts = int(time.time()) - (window_minutes * 60)

        # Track token addresses yang dibeli KOL dalam window ini
        # (KOL adalah persona berbeda dari smart money — tidak perlu overlap wallet)
        kol_tokens: set[str] = set()
        for trade in kol_trades:
            token_addr_kol = trade.get("base_address", "")
            ts = _extract_ts(trade)
            if token_addr_kol and ts >= cutoff_ts:
                kol_tokens.add(token_addr_kol.lower())

        # Group smart money trades by token
        # Structure: token_address → list of (wallet, ts, usd_value)
        token_trades: dict[str, list[tuple[str, int, float]]] = {}
        for trade in sm_trades:
            token_addr = trade.get("base_address", "")
            wallet = _extract_wallet(trade)
            ts = _extract_ts(trade)
            usd_value = float(trade.get("usd_value") or trade.get("total_value") or 0.0)

            if not token_addr or not wallet:
                continue
            if ts < cutoff_ts:
                continue  # Terlalu lama, skip

            token_addr_lower = token_addr.lower()
            if token_addr_lower not in token_trades:
                token_trades[token_addr_lower] = []
            token_trades[token_addr_lower].append((wallet, ts, usd_value))

        # Build cluster signals
        clusters: list[ClusterSignal] = []
        for token_addr, trade_list in token_trades.items():
            # Unique wallets
            seen_wallets: set[str] = set()
            unique_entries: list[tuple[str, int, float]] = []
            for wallet, ts, usd in trade_list:
                wallet_lower = wallet.lower()
                if wallet_lower not in seen_wallets:
                    seen_wallets.add(wallet_lower)
                    unique_entries.append((wallet, ts, usd))

            wallet_count = len(unique_entries)
            if wallet_count < 3:
                # Tidak memenuhi threshold cluster — skip
                # (caller bisa filter MEDIUM/WEAK dari output kalau mau, tapi
                # detect_clusters hanya return >= 3 per spec)
                continue

            timestamps = [ts for _, ts, _ in unique_entries]
            wallets = [w for w, _, _ in unique_entries]
            total_usd = sum(usd for _, _, usd in unique_entries)
            earliest_ts = min(timestamps)
            latest_ts = max(timestamps)
            window_secs = latest_ts - earliest_ts

            # Check KOL participation: apakah ada KOL yang beli token ini dalam window?
            kol_in_cluster = token_addr in kol_tokens

            # Window untuk strength calculation: pakai window_secs / 60 capped ke window_minutes
            effective_window_min = min(window_secs // 60, window_minutes) if window_secs > 0 else 0

            strength = signal_strength_from_count(
                wallet_count=wallet_count,
                kol_participation=kol_in_cluster,
                window_minutes=effective_window_min,
            )

            clusters.append(
                ClusterSignal(
                    token_address=token_addr,
                    chain=chain,
                    wallet_count=wallet_count,
                    total_usd=total_usd,
                    earliest_buy_ts=earliest_ts,
                    latest_buy_ts=latest_ts,
                    time_window_seconds=window_secs,
                    wallet_addresses=wallets,
                    strength=strength,
                    kol_participation=kol_in_cluster,
                )
            )

        # Sort: strength desc, lalu total_usd desc
        clusters.sort(
            key=lambda c: (_STRENGTH_ORDER[c.strength], c.total_usd),
            reverse=True,
        )

        log.info(
            "cluster_detect_done",
            chain=chain,
            clusters_found=len(clusters),
            very_strong=[c.token_address for c in clusters if c.strength == "VERY_STRONG"],
        )
        return clusters

    async def get_cluster_for_token(
        self,
        token_address: str,
        chain: str = "sol",
        window_minutes: int = 30,
    ) -> ClusterSignal | None:
        """
        Fetch cluster signal spesifik untuk satu token.

        Lebih efisien dari detect_clusters() bila hanya butuh satu token.
        Returns None jika wallet_count < 3 (tidak ada cluster).

        Args:
            token_address: Solana token address yang dicek.
            chain: Chain target.
            window_minutes: Window waktu untuk cluster detection.
        """
        log.debug(
            "cluster_token_check",
            token=token_address,
            chain=chain,
            window_minutes=window_minutes,
        )

        # Fetch paralel
        sm_trades, kol_trades = await asyncio.gather(
            self._gmgn.get_smart_money_trades(chain=chain, limit=200, side="buy"),
            self._gmgn.get_kol_trades(chain=chain, limit=200, side="buy"),
        )

        cutoff_ts = int(time.time()) - (window_minutes * 60)
        token_lower = token_address.lower()

        # Apakah ada KOL yang beli token yang sama dalam window ini?
        kol_bought_token = False
        for trade in kol_trades:
            base_addr = trade.get("base_address", "")
            ts = _extract_ts(trade)
            if base_addr.lower() == token_lower and ts >= cutoff_ts:
                kol_bought_token = True
                break

        # Smart money buys untuk token ini
        seen_wallets: set[str] = set()
        entries: list[tuple[str, int, float]] = []

        for trade in sm_trades:
            base_addr = trade.get("base_address", "")
            if base_addr.lower() != token_lower:
                continue

            wallet = _extract_wallet(trade)
            ts = _extract_ts(trade)
            usd = float(trade.get("usd_value") or trade.get("total_value") or 0.0)

            if not wallet or ts < cutoff_ts:
                continue

            wallet_lower = wallet.lower()
            if wallet_lower not in seen_wallets:
                seen_wallets.add(wallet_lower)
                entries.append((wallet, ts, usd))

        wallet_count = len(entries)
        if wallet_count < 3:
            return None

        timestamps = [ts for _, ts, _ in entries]
        wallets = [w for w, _, _ in entries]
        total_usd = sum(usd for _, _, usd in entries)
        earliest_ts = min(timestamps)
        latest_ts = max(timestamps)
        window_secs = latest_ts - earliest_ts

        kol_in_cluster = kol_bought_token
        effective_window_min = min(window_secs // 60, window_minutes) if window_secs > 0 else 0

        strength = signal_strength_from_count(
            wallet_count=wallet_count,
            kol_participation=kol_in_cluster,
            window_minutes=effective_window_min,
        )

        return ClusterSignal(
            token_address=token_address,
            chain=chain,
            wallet_count=wallet_count,
            total_usd=total_usd,
            earliest_buy_ts=earliest_ts,
            latest_buy_ts=latest_ts,
            time_window_seconds=window_secs,
            wallet_addresses=wallets,
            strength=strength,
            kol_participation=kol_in_cluster,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_wallet(trade: dict) -> str:
    """Extract wallet address dari berbagai trade schema GMGN."""
    return (
        trade.get("maker_info", {}).get("address", "")
        or trade.get("wallet", "")
        or trade.get("user_address", "")
        or ""
    )


def _extract_ts(trade: dict) -> int:
    """Extract Unix timestamp dari trade record."""
    return int(trade.get("timestamp") or trade.get("block_time") or 0)
