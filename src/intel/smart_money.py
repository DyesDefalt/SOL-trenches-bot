"""
Smart Money Aggregator — unified intelligence layer combining Nansen + GMGN signals.

Menggabungkan dua sumber data utama:
1. Nansen: on-chain netflow analysis, smart trader labels, fund flows
2. GMGN: real-time smart money trades, KOL activity, cluster detection

Output tunggal: SmartMoneySignal dengan composite_score_bonus dan verdict.

# Scoring Logic:
- Nansen trend dictates base score (sustained_accumulation = +20, distribution = -25)
- GMGN cluster (3+ smart money dalam 15m) = additive +15
- Kombinasi max (GMGN cluster + sustained_accumulation) = +30

# Graceful Degradation:
- Nansen tidak return data (token belum ditrack) → fallback GMGN-only dengan
  confidence lebih rendah. Verdict tetap dihitung dari GMGN signals saja.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from src.infra.logger import get_logger

if TYPE_CHECKING:
    from src.clients.gmgn import GMGNClient
    from src.intel.nansen_client import NansenClient

log = get_logger(__name__)

Verdict = Literal["STRONG_BUY", "BUY", "NEUTRAL", "AVOID", "STRONG_AVOID"]
NansenTrend = Literal[
    "sustained_accumulation",
    "fresh_entry",
    "reducing",
    "distribution",
    "mixed",
    "unknown",
]


@dataclass
class SmartMoneySignal:
    """
    Aggregated smart money signal untuk satu token.

    Menggabungkan Nansen (institutional flow) + GMGN (retail smart money)
    menjadi composite_score_bonus dan verdict yang bisa langsung dipakai
    scoring engine.

    composite_score_bonus: float dari -30 s/d +30, dijumlahkan ke bot score.
    verdict: categorical summary untuk quick decision.
    nansen_available: False jika Nansen tidak punya data untuk token ini
                      (token terlalu baru atau belum ditrack).
    """

    token_address: str
    chain: str = "sol"

    # --- Nansen signals ---
    nansen_netflow_1h: float = 0.0
    nansen_netflow_24h: float = 0.0
    nansen_netflow_7d: float = 0.0
    nansen_netflow_30d: float = 0.0
    nansen_trend: NansenTrend = "unknown"
    nansen_smart_trader_flow: float = 0.0
    nansen_fund_flow: float = 0.0
    nansen_whale_flow: float = 0.0
    nansen_fresh_wallet_flow: float = 0.0
    nansen_available: bool = False  # True jika Nansen punya data untuk token ini

    # --- GMGN signals ---
    gmgn_smart_money_count_15m: int = 0
    gmgn_kol_count_15m: int = 0
    gmgn_smart_money_buyers: list[str] = field(default_factory=list)

    # --- Combined verdict ---
    composite_score_bonus: float = 0.0  # -30 to +30 contribution to bot score
    verdict: Verdict = "NEUTRAL"


def _compute_composite_score(signal: SmartMoneySignal) -> float:
    """
    Hitung composite_score_bonus dari kombinasi Nansen + GMGN signals.

    Rules (dari spec):
    - sustained_accumulation: +20
    - fresh_entry + smart_trader_flow > 0: +15
    - fresh_entry tanpa konfirmasi smart_trader: +5
    - reducing: -10
    - distribution: -25
    - GMGN cluster (3+ smart money 15m): +15 (additive)
    - GMGN cluster + sustained_accumulation: max +30 (capped di sini)
    - KOL count > 2 tapi 0 smart money: -5 (FOMO warning)
    """
    score: float = 0.0

    # === Nansen contribution ===
    trend = signal.nansen_trend
    if trend == "sustained_accumulation":
        score += 20.0
    elif trend == "fresh_entry":
        if signal.nansen_smart_trader_flow > 0:
            score += 15.0
        else:
            score += 5.0
    elif trend == "reducing":
        score += -10.0
    elif trend == "distribution":
        score += -25.0
    # "mixed" dan "unknown" → 0 dari Nansen

    # === GMGN contribution ===
    has_cluster = signal.gmgn_smart_money_count_15m >= 3
    kol_only_fomo = signal.gmgn_kol_count_15m > 2 and signal.gmgn_smart_money_count_15m == 0

    if has_cluster:
        # Capped at +30 kalau sudah dapat +20 dari Nansen accumulated
        if trend == "sustained_accumulation":
            # Kombinasi max → score langsung naik ke 30 (spec: max bonus)
            score = 30.0
        else:
            score += 15.0
    elif kol_only_fomo:
        score += -5.0  # FOMO warning, KOL tanpa smart money backing

    # Cap ke range -30..+30
    return max(-30.0, min(30.0, score))


def _compute_verdict(composite_score: float) -> Verdict:
    """Mapping composite_score → verdict. Thresholds sesuai spec."""
    if composite_score >= 25:
        return "STRONG_BUY"
    if composite_score >= 10:
        return "BUY"
    if composite_score >= -5:
        return "NEUTRAL"
    if composite_score >= -15:
        return "AVOID"
    return "STRONG_AVOID"


class SmartMoneyAggregator:
    """
    Unified Smart Money interface menggabungkan Nansen + GMGN signals.

    Fetch dari kedua sumber secara paralel (asyncio.gather), combine menjadi
    SmartMoneySignal tunggal dengan composite scoring.

    Usage:
        aggregator = SmartMoneyAggregator(nansen_client, gmgn_client, registry)
        signal = await aggregator.get_signal("TokenAddressXxx", chain="sol")
        if signal.verdict == "STRONG_BUY":
            # proceed with entry logic
    """

    def __init__(
        self,
        nansen_client: "NansenClient",
        gmgn_client: "GMGNClient",
        registry: object,  # SmartWalletRegistry — avoid circular import
    ) -> None:
        self._nansen = nansen_client
        self._gmgn = gmgn_client
        self._registry = registry

    async def get_signal(
        self,
        token_address: str,
        chain: str = "sol",
    ) -> SmartMoneySignal:
        """
        Fetch Nansen + GMGN signals secara paralel, gabung jadi single SmartMoneySignal.

        Graceful handling:
        - Nansen error / no data → fallback ke GMGN-only, nansen_available=False
        - GMGN error → hanya Nansen data yang dipakai
        - Keduanya error → return NEUTRAL signal dengan composite_score=0
        """
        log.info(
            "smart_money_signal_start",
            token=token_address,
            chain=chain,
        )

        # Fetch paralel — tidak saling menunggu
        nansen_result, gmgn_result = await asyncio.gather(
            self._fetch_nansen(token_address, chain),
            self._fetch_gmgn(token_address, chain),
            return_exceptions=True,
        )

        signal = SmartMoneySignal(token_address=token_address, chain=chain)

        # Populate Nansen fields
        if isinstance(nansen_result, Exception):
            log.warning(
                "nansen_fetch_error",
                token=token_address,
                error=str(nansen_result),
            )
        elif nansen_result is not None:
            self._apply_nansen(signal, nansen_result)

        # Populate GMGN fields
        if isinstance(gmgn_result, Exception):
            log.warning(
                "gmgn_fetch_error",
                token=token_address,
                error=str(gmgn_result),
            )
        elif gmgn_result is not None:
            self._apply_gmgn(signal, gmgn_result)

        # Compute final scores
        signal.composite_score_bonus = _compute_composite_score(signal)
        signal.verdict = _compute_verdict(signal.composite_score_bonus)

        log.info(
            "smart_money_signal_done",
            token=token_address,
            trend=signal.nansen_trend,
            cluster=signal.gmgn_smart_money_count_15m,
            score=signal.composite_score_bonus,
            verdict=signal.verdict,
            nansen_available=signal.nansen_available,
        )
        return signal

    async def _fetch_nansen(
        self,
        token_address: str,
        chain: str,
    ) -> dict | None:
        """
        Fetch flow intelligence dari Nansen untuk token ini.

        Returns None jika token tidak ditrack Nansen (baru / unlisted).
        Menggunakan get_flow_intelligence yang lebih kaya dibanding raw netflow.
        """
        try:
            # Map chain "sol" → "solana" untuk Nansen API
            nansen_chain = "solana" if chain == "sol" else chain
            flow = await self._nansen.get_flow_intelligence(
                chain=nansen_chain,
                token_address=token_address,
                timeframe="1d",
            )
            if not flow:
                return None

            # Juga ambil dex trades untuk extract per-category flows
            trades = await self._nansen.get_smart_money_dex_trades_for_token(
                chain=nansen_chain,
                token_address=token_address,
                limit=50,
            )
            return {"flow": flow, "trades": trades}
        except Exception as e:
            log.debug(
                "nansen_no_data",
                token=token_address,
                reason=str(e),
            )
            return None

    async def _fetch_gmgn(
        self,
        token_address: str,
        chain: str,
    ) -> dict | None:
        """
        Fetch recent smart money + KOL trades dari GMGN, filter ke token ini.

        Returns dict dengan smart_money_buyers dan kol_count.
        """
        # Fetch paralel smart money + KOL
        sm_trades, kol_trades = await asyncio.gather(
            self._gmgn.get_smart_money_trades(chain=chain, limit=200, side="buy"),
            self._gmgn.get_kol_trades(chain=chain, limit=200, side="buy"),
        )

        import time
        cutoff_ts = int(time.time()) - (15 * 60)  # 15 menit lookback

        # Filter ke token ini
        sm_buyers: set[str] = set()
        for trade in sm_trades:
            base_addr = trade.get("base_address", "")
            ts = int(trade.get("timestamp") or trade.get("block_time") or 0)
            wallet = (
                trade.get("maker_info", {}).get("address")
                or trade.get("wallet")
                or ""
            )
            if base_addr.lower() == token_address.lower() and ts >= cutoff_ts and wallet:
                sm_buyers.add(wallet)

        kol_count = 0
        for trade in kol_trades:
            base_addr = trade.get("base_address", "")
            ts = int(trade.get("timestamp") or trade.get("block_time") or 0)
            if base_addr.lower() == token_address.lower() and ts >= cutoff_ts:
                kol_count += 1

        return {
            "smart_money_buyers": sorted(sm_buyers),
            "kol_count": kol_count,
        }

    def _apply_nansen(self, signal: SmartMoneySignal, data: dict) -> None:
        """Populate Nansen fields pada SmartMoneySignal dari hasil fetch."""
        flow = data.get("flow", {})
        trades = data.get("trades", [])

        if not flow:
            return

        signal.nansen_available = True
        signal.nansen_netflow_1h = float(flow.get("netflow_1h", 0.0))
        signal.nansen_netflow_24h = float(flow.get("netflow_24h", 0.0))
        signal.nansen_netflow_7d = float(flow.get("netflow_7d", 0.0))
        signal.nansen_netflow_30d = float(flow.get("netflow_30d", 0.0))
        signal.nansen_trend = flow.get("trend", "unknown")

        # Extract per-label flows dari trades
        label_flow: dict[str, float] = {}
        for trade in trades:
            label = trade.get("label", "")
            usd = float(trade.get("usd_value", 0.0))
            side = trade.get("side", "")
            delta = usd if side == "buy" else -usd
            label_flow[label] = label_flow.get(label, 0.0) + delta

        signal.nansen_fund_flow = label_flow.get("fund", 0.0)
        signal.nansen_smart_trader_flow = label_flow.get("smart_trader", 0.0)
        signal.nansen_whale_flow = label_flow.get("whale", 0.0)
        signal.nansen_fresh_wallet_flow = label_flow.get("fresh_wallet", 0.0)

    def _apply_gmgn(self, signal: SmartMoneySignal, data: dict) -> None:
        """Populate GMGN fields pada SmartMoneySignal."""
        buyers = data.get("smart_money_buyers", [])
        signal.gmgn_smart_money_buyers = buyers
        signal.gmgn_smart_money_count_15m = len(buyers)
        signal.gmgn_kol_count_15m = int(data.get("kol_count", 0))
