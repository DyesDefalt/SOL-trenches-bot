"""
Pump.fun Graduation Tracker — Phase 7e.

Tracks status token di Pump.fun bonding curve:
  - Apakah token ini token Pump.fun?
  - Sudah seberapa jauh bonding curve (graduation_pct)?
  - Apakah sudah graduate ke Raydium (dan karena itu sudah terlambat)?

Score bonus untuk scoring engine:
  - Sweet spot (70-95%): +10  ← target utama bot
  - 50-70%: +5
  - 30-50%: +2
  - <30% (terlalu awal, volatile): 0
  - Sudah graduated: -5  (sudah pumped, bukan target strategi)

Cache TTL pendek (30s) — graduation_pct berubah cepat saat near completion.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.infra.cache import cached
from src.infra.logger import get_logger

if TYPE_CHECKING:
    from src.intel.pumpfun_client import PumpfunClient

log = get_logger(__name__)


@dataclass
class GraduationStatus:
    """Status graduation token di Pump.fun bonding curve."""

    token_address: str
    is_pumpfun: bool  # apakah ini token Pump.fun sama sekali?
    graduation_pct: float = 0.0   # 0-100, 100 = graduated
    market_cap_usd: float = 0.0
    is_graduated: bool = False
    is_in_sweet_spot: bool = False  # 70-95% per spec
    score_bonus: float = 0.0       # untuk scoring engine


def _compute_score_bonus(status: GraduationStatus) -> float:
    """
    Hitung score bonus berdasarkan posisi di bonding curve.

    Logic:
    - Tidak relevan (bukan pumpfun): 0
    - Sudah graduated (pindah ke Raydium): -5 (sudah pumped)
    - Sweet spot 70-95%: +10 (target utama pre-graduation snipe)
    - 50-70%: +5
    - 30-50%: +2
    - <30% (terlalu awal, risiko tinggi): 0
    """
    if not status.is_pumpfun:
        return 0.0
    if status.is_graduated:
        return -5.0

    pct = status.graduation_pct
    if 70.0 <= pct <= 95.0:
        return 10.0
    if 50.0 <= pct < 70.0:
        return 5.0
    if 30.0 <= pct < 50.0:
        return 2.0
    # < 30%: terlalu awal atau > 95% (hampir lulus tapi belum sweet spot)
    return 0.0


class PumpfunTracker:
    """
    Checks graduation status token di Pump.fun.

    Usage:
        tracker = PumpfunTracker(pumpfun_client)
        status = await tracker.check("TokenMintAddress...")
        if status.is_in_sweet_spot:
            # Token lagi di 70-95% bonding curve — potensial snipe
            score += status.score_bonus
    """

    def __init__(self, pumpfun_client: PumpfunClient) -> None:
        self._client = pumpfun_client

    @cached(prefix="pumpfun_grad:", ttl=30)
    async def check(self, token_address: str) -> GraduationStatus:
        """
        Check graduation status token. Returns GraduationStatus.

        Kalau token bukan Pump.fun (404 atau None dari client) → is_pumpfun=False.
        """
        log.debug("pumpfun_check_start", token=token_address)

        try:
            token_info = await self._client.get_token_info(token_address)
        except Exception as exc:
            log.warning("pumpfun_fetch_error", token=token_address, error=str(exc))
            token_info = None

        if token_info is None:
            log.debug("pumpfun_not_found", token=token_address)
            return GraduationStatus(
                token_address=token_address,
                is_pumpfun=False,
            )

        # Compute graduation percentage via client helper
        try:
            graduation_pct = float(self._client.graduation_progress_pct(token_info))
        except Exception:
            graduation_pct = 0.0

        try:
            is_graduated = bool(self._client.is_graduated(token_info))
        except Exception:
            is_graduated = False

        try:
            is_in_sweet_spot = bool(self._client.is_in_sweet_spot(token_info))
        except Exception:
            # Fallback manual: 70-95%
            is_in_sweet_spot = 70.0 <= graduation_pct <= 95.0

        # Market cap dari token_info (key varies per API)
        market_cap_usd = float(
            token_info.get("usd_market_cap")
            or token_info.get("market_cap_usd")
            or token_info.get("market_cap")
            or 0.0
        )

        status = GraduationStatus(
            token_address=token_address,
            is_pumpfun=True,
            graduation_pct=graduation_pct,
            market_cap_usd=market_cap_usd,
            is_graduated=is_graduated,
            is_in_sweet_spot=is_in_sweet_spot,
            score_bonus=0.0,  # akan diisi setelah compute
        )
        status.score_bonus = _compute_score_bonus(status)

        log.debug(
            "pumpfun_check_done",
            token=token_address,
            graduation_pct=graduation_pct,
            is_graduated=is_graduated,
            is_in_sweet_spot=is_in_sweet_spot,
            score_bonus=status.score_bonus,
        )
        return status
