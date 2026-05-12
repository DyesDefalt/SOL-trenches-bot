"""
Smart Wallet Registry — discovery, classification, persistence.

Inilah jantung sistem signal scoring. Tanpa list smart wallet yang berkualitas,
sinyal "X smart money beli token Y" tidak punya makna.

# Strategi Discovery (3 lapis):

1. **Auto-discovery dari GMGN** (utama, refresh tiap 6 jam):
   - Fetch recent smart money trades + KOL trades dari endpoint GMGN
   - Extract unique wallet addresses dari trade history
   - Untuk tiap candidate, classify tier pakai stats 30d (winrate + realized profit)

2. **Manual additions** (high-confidence, dari riset user):
   - User bisa tambah wallet via `add_manual()` atau edit `data/smart_wallets_manual.json`
   - Override automatic classification (force-tier)
   - Use case: alpha trader dari Twitter, KOL terverifikasi, yunus-tier wallet

3. **Blacklist** (defensive):
   - Wallet yang detected gaming sistem (split modal ke banyak alamat, wash trading)
   - Edit `data/smart_wallets_blacklist.json`

# Tier Classification (per spec asli):

- **A-Tier**: winrate ≥ 65% AND realized_profit ≥ 30 SOL (30 hari) — skor 35% weight max
- **B-Tier**: winrate 55-64% — track normal
- **C-Tier**: winrate 45-54% — backup only, exclude dari scoring kalau jumlah cukup A+B
- **F-Tier**: skip, jangan track

# Persistence:

Phase 1 pakai JSON file (`data/smart_wallets.json`) — simple, no DB dependency.
Phase 4 migrate ke Postgres saat schema tersedia.

# Rate-Limit Safe:

Refresh full (200 candidates × 1 stats call each) = ~200 calls weight=3 = 60s di GMGN
free tier. Run scheduled, jangan on-demand di hot path.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from src.clients.gmgn import GMGNClient
from src.config import settings
from src.infra.logger import get_logger

log = get_logger(__name__)

Tier = Literal[
    "A",
    "B",
    "C",
    "F",
    "MANUAL_A",
    "MANUAL_B",
    "BLACKLIST",
    # Nansen-sourced tiers (Phase 7c)
    "NANSEN_FUND",         # On-chain funds, highest conviction
    "NANSEN_SM_ALL_TIME",  # Nansen "Smart Trader" label — all-time track record
    "NANSEN_SM_HOT_30D",   # "30D Smart Trader" — hot hands bulan ini
    "NANSEN_SM_HOT_90D",   # "90D Smart Trader" — hot hands 3 bulan
    "NANSEN_KOL",          # "Public Figure" label — social signal, bukan pure smart money
]
DEFAULT_REGISTRY_PATH = Path("data/smart_wallets.json")
MANUAL_PATH = Path("data/smart_wallets_manual.json")
BLACKLIST_PATH = Path("data/smart_wallets_blacklist.json")


@dataclass
class SmartWallet:
    """Single smart wallet record dengan klasifikasi + stats."""

    address: str
    tier: Tier
    chain: str = "sol"
    winrate: float = 0.0  # 0..1
    realized_profit: float = 0.0  # native unit (SOL atau USD per GMGN response)
    total_profit: float = 0.0
    buy_count: int = 0
    sell_count: int = 0
    token_num: int = 0
    last_updated: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    source: str = "auto"  # "auto" | "manual" | "blacklist"
    notes: str = ""

    @property
    def is_active(self) -> bool:
        """Apakah wallet ini diaktifkan untuk scoring."""
        return self.tier in (
            "A", "B", "C",
            "MANUAL_A", "MANUAL_B",
            "NANSEN_FUND", "NANSEN_SM_ALL_TIME", "NANSEN_SM_HOT_30D",
            "NANSEN_SM_HOT_90D", "NANSEN_KOL",
        )

    @property
    def is_top_tier(self) -> bool:
        """
        A atau B tier (dan Nansen equivalents) — yang ditrack via WebSocket real-time.

        NANSEN_KOL sengaja dikecualikan dari top-tier: ia social signal bukan smart money.
        """
        return self.tier in (
            "A", "B",
            "MANUAL_A", "MANUAL_B",
            "NANSEN_FUND", "NANSEN_SM_ALL_TIME", "NANSEN_SM_HOT_30D", "NANSEN_SM_HOT_90D",
        )

    @property
    def score_multiplier(self) -> float:
        """
        Bobot kontribusi ke score signal.

        Nansen tiers:
        - NANSEN_FUND: 1.2 — institutional fund, tertinggi (di atas MANUAL_A)
        - NANSEN_SM_HOT_30D: 1.1 — hot hands, slight boost karena recency
        - NANSEN_SM_ALL_TIME / NANSEN_SM_HOT_90D: 1.0 — setara A-tier
        - NANSEN_KOL: 0.6 — social signal only, lebih rendah dari B-tier

        Manual override = same as auto tier counterpart.
        """
        if self.tier == "NANSEN_FUND":
            return 1.2
        if self.tier in ("A", "MANUAL_A", "NANSEN_SM_ALL_TIME", "NANSEN_SM_HOT_90D"):
            return 1.0
        if self.tier == "NANSEN_SM_HOT_30D":
            return 1.1
        if self.tier in ("B", "MANUAL_B"):
            return 0.7
        if self.tier == "NANSEN_KOL":
            return 0.6
        if self.tier == "C":
            return 0.4
        return 0.0


def _classify_tier_from_stats(
    winrate: float,
    realized_profit: float,
    min_trades: int,
    buy_count: int,
    sell_count: int,
) -> Tier:
    """
    Klasifikasi otomatis berdasarkan stats GMGN.

    Filter awal: minimal trade count (hindari wallet baru yang stats-nya unstable).
    """
    if (buy_count + sell_count) < min_trades:
        return "F"

    if winrate >= 0.65 and realized_profit >= 30:
        return "A"
    if winrate >= 0.55:
        # Winrate cukup tapi profit belum 30 SOL → B-tier (winrate is primary signal)
        return "B"
    if winrate >= 0.45:
        return "C"
    return "F"


class SmartWalletRegistry:
    """
    Persistent registry smart wallet dengan auto-refresh dari GMGN.

    Usage:
        registry = SmartWalletRegistry()
        await registry.load()  # load dari disk

        # Bootstrap pertama kali (atau full re-discovery)
        async with GMGNClient() as gmgn:
            await registry.bootstrap_from_gmgn(gmgn, sample_size=200)

        # Routine refresh tiap 6 jam (re-classify existing + add new from recent trades)
        await registry.refresh(gmgn)

        # Use untuk scoring engine
        active = registry.get_active_wallets()  # all A+B+C
        top = registry.get_top_tier_wallets()   # A+B only — untuk WS subscribe
    """

    def __init__(
        self,
        registry_path: Path = DEFAULT_REGISTRY_PATH,
        manual_path: Path = MANUAL_PATH,
        blacklist_path: Path = BLACKLIST_PATH,
        min_trades_for_classification: int = 30,
    ) -> None:
        self.registry_path = registry_path
        self.manual_path = manual_path
        self.blacklist_path = blacklist_path
        self.min_trades = min_trades_for_classification

        # In-memory state — keyed by lowercase address untuk consistency
        self._wallets: dict[str, SmartWallet] = {}

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    async def load(self) -> int:
        """
        Load registry dari disk + apply manual overrides + blacklist.

        Returns: jumlah wallet active loaded.
        """
        # 1. Load auto-discovered
        if self.registry_path.exists():
            try:
                data = json.loads(self.registry_path.read_text())
                for raw in data:
                    sw = SmartWallet(**raw)
                    self._wallets[sw.address.lower()] = sw
                log.info("registry_loaded", path=str(self.registry_path), count=len(self._wallets))
            except (json.JSONDecodeError, TypeError) as e:
                log.error("registry_load_failed", error=str(e))

        # 2. Apply manual additions (override auto kalau ada conflict)
        await self._load_manual()

        # 3. Apply blacklist (override semuanya)
        await self._load_blacklist()

        return sum(1 for w in self._wallets.values() if w.is_active)

    async def _load_manual(self) -> None:
        """Manual smart wallets — selalu override auto."""
        if not self.manual_path.exists():
            return
        try:
            data = json.loads(self.manual_path.read_text())
            for entry in data:
                addr = entry["address"].lower()
                tier_input = entry.get("tier", "A").upper()
                tier: Tier = "MANUAL_A" if tier_input == "A" else "MANUAL_B"

                self._wallets[addr] = SmartWallet(
                    address=entry["address"],
                    tier=tier,
                    chain=entry.get("chain", "sol"),
                    source="manual",
                    notes=entry.get("notes", ""),
                )
            log.info("manual_wallets_loaded", count=len(data))
        except (json.JSONDecodeError, KeyError) as e:
            log.error("manual_load_failed", error=str(e))

    async def _load_blacklist(self) -> None:
        """Blacklist — override semuanya jadi BLACKLIST tier."""
        if not self.blacklist_path.exists():
            return
        try:
            data = json.loads(self.blacklist_path.read_text())
            for entry in data:
                addr = entry["address"].lower() if isinstance(entry, dict) else entry.lower()
                if addr in self._wallets:
                    self._wallets[addr].tier = "BLACKLIST"
                    self._wallets[addr].source = "blacklist"
                else:
                    self._wallets[addr] = SmartWallet(
                        address=addr,
                        tier="BLACKLIST",
                        source="blacklist",
                    )
            log.info("blacklist_loaded", count=len(data))
        except (json.JSONDecodeError, KeyError) as e:
            log.error("blacklist_load_failed", error=str(e))

    async def save(self) -> None:
        """Persist auto-discovered wallets ke disk (skip manual + blacklist)."""
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        # Skip manual + blacklist — itu source-of-truth di file terpisah
        to_save = [
            asdict(w)
            for w in self._wallets.values()
            if w.source == "auto" and w.tier not in ("BLACKLIST",)
        ]
        self.registry_path.write_text(json.dumps(to_save, indent=2))
        log.info("registry_saved", path=str(self.registry_path), count=len(to_save))

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------
    async def bootstrap_from_gmgn(
        self,
        gmgn: GMGNClient,
        sample_size: int = 200,
        chain: str = "sol",
    ) -> dict[Tier, int]:
        """
        Full bootstrap: fetch recent smart money trades + KOL trades, extract unique
        wallets, classify each, persist.

        Returns: dict tier_count untuk visibility (e.g., {"A": 12, "B": 34, ...}).

        Cost (GMGN free tier):
        - 2 calls @ weight=1 (smart money + kol) → ~0.2s
        - sample_size unique wallets × 1 stats call @ weight=3 → sample_size×0.3s
        - Untuk sample_size=200: total ~60 detik. Run scheduled, bukan on-demand.
        """
        log.info("bootstrap_start", sample_size=sample_size, chain=chain)
        candidates = await self._collect_candidates(gmgn, chain, sample_size)
        log.info("bootstrap_candidates_found", count=len(candidates))

        tier_counts: dict[Tier, int] = {"A": 0, "B": 0, "C": 0, "F": 0}
        for wallet in candidates:
            # Skip kalau sudah ada di manual (jangan override) atau blacklist
            existing = self._wallets.get(wallet.lower())
            if existing and existing.source in ("manual", "blacklist"):
                continue

            try:
                stats = await gmgn.get_wallet_stats(wallet, chain=chain, period="30d")
            except Exception as e:
                log.warning("stats_fetch_failed", wallet=wallet, error=str(e))
                continue

            if not stats:
                continue

            winrate = float(stats.get("winrate", 0))
            realized = float(stats.get("realized_profit", 0))
            total = float(stats.get("total_profit", 0))
            buy_count = int(stats.get("buy_count", 0))
            sell_count = int(stats.get("sell_count", 0))
            token_num = int(stats.get("token_num", 0))

            tier = _classify_tier_from_stats(
                winrate=winrate,
                realized_profit=realized,
                min_trades=self.min_trades,
                buy_count=buy_count,
                sell_count=sell_count,
            )

            sw = SmartWallet(
                address=wallet,
                tier=tier,
                chain=chain,
                winrate=winrate,
                realized_profit=realized,
                total_profit=total,
                buy_count=buy_count,
                sell_count=sell_count,
                token_num=token_num,
                source="auto",
            )
            self._wallets[wallet.lower()] = sw
            tier_counts[tier] = tier_counts.get(tier, 0) + 1

        await self.save()
        log.info("bootstrap_done", tier_counts=tier_counts)
        return tier_counts

    async def _collect_candidates(
        self,
        gmgn: GMGNClient,
        chain: str,
        sample_size: int,
    ) -> list[str]:
        """Fetch recent trades dari GMGN, extract unique wallet addresses."""
        # Smart money trades — primary source
        smart_trades = await gmgn.get_smart_money_trades(chain=chain, limit=200)
        # KOL trades — secondary, untuk diversity
        kol_trades = await gmgn.get_kol_trades(chain=chain, limit=200)

        # Extract addresses dari maker_info atau wallet field
        addresses: list[str] = []
        seen: set[str] = set()
        for trade in [*smart_trades, *kol_trades]:
            wallet = (
                trade.get("maker_info", {}).get("address")
                or trade.get("wallet")
                or trade.get("user_address")
                or ""
            )
            if not wallet:
                continue
            wallet_lower = wallet.lower()
            if wallet_lower in seen:
                continue
            seen.add(wallet_lower)
            addresses.append(wallet)
            if len(addresses) >= sample_size:
                break

        return addresses

    async def refresh(
        self,
        gmgn: GMGNClient,
        chain: str = "sol",
        max_age_hours: int = 6,
    ) -> dict[Tier, int]:
        """
        Re-classify existing wallets yang sudah lama tidak di-update PLUS add new
        candidates dari recent trades.

        Lebih efisien daripada bootstrap karena skip wallet yang fresh.
        """
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_hours * 3600
        stale_wallets = [
            sw.address
            for sw in self._wallets.values()
            if sw.source == "auto"
            and datetime.fromisoformat(sw.last_updated).timestamp() < cutoff
        ]
        log.info("refresh_start", stale_count=len(stale_wallets))

        # Re-classify stale
        for wallet in stale_wallets:
            try:
                stats = await gmgn.get_wallet_stats(wallet, chain=chain, period="30d")
            except Exception as e:
                log.warning("refresh_stats_failed", wallet=wallet, error=str(e))
                continue

            if stats:
                self._update_wallet_from_stats(wallet, stats, chain)

        # Add new candidates dari latest trades
        new_count = 0
        candidates = await self._collect_candidates(gmgn, chain, sample_size=100)
        for wallet in candidates:
            if wallet.lower() in self._wallets:
                continue
            try:
                stats = await gmgn.get_wallet_stats(wallet, chain=chain, period="30d")
            except Exception:
                continue
            if stats:
                self._update_wallet_from_stats(wallet, stats, chain)
                new_count += 1

        await self.save()
        tier_counts = self._tier_counts()
        log.info("refresh_done", new_added=new_count, tier_counts=tier_counts)
        return tier_counts

    async def enrich_from_nansen(self, nansen_client: object) -> dict[str, int]:
        """
        Fetch top smart money wallets dari Nansen, tambah ke registry dengan tier Nansen.

        Strategy:
        1. Fetch top wallet dari get_smart_money_netflow (chains=["solana"], limit=200)
        2. Map Nansen labels ke tier:
           - "fund" / "fund_manager" → NANSEN_FUND
           - "smart_trader" → NANSEN_SM_ALL_TIME
           - "30d_smart_trader" → NANSEN_SM_HOT_30D
           - "90d_smart_trader" → NANSEN_SM_HOT_90D
           - "public_figure" / "kol" → NANSEN_KOL
        3. Skip wallet yang sudah ada di manual atau blacklist (preserve override)
        4. Hanya tambah wallet dengan positive netflow (net buyers)
        5. Save ke disk

        Returns:
            dict tier → jumlah wallet yang ditambahkan. Contoh: {"NANSEN_FUND": 3, ...}
        """
        log.info("nansen_enrich_start")

        try:
            netflow_data = await nansen_client.get_smart_money_netflow(  # type: ignore[attr-defined]
                chains=["solana"],
                limit=200,
            )
        except Exception as e:
            log.error("nansen_netflow_fetch_failed", error=str(e))
            return {}

        # Map Nansen label string → Tier
        _NANSEN_LABEL_MAP: dict[str, Tier] = {
            "fund": "NANSEN_FUND",
            "fund_manager": "NANSEN_FUND",
            "smart_trader": "NANSEN_SM_ALL_TIME",
            "30d_smart_trader": "NANSEN_SM_HOT_30D",
            "90d_smart_trader": "NANSEN_SM_HOT_90D",
            "public_figure": "NANSEN_KOL",
            "kol": "NANSEN_KOL",
        }

        tier_counts: dict[str, int] = {}

        for entry in netflow_data:
            wallet_addr = entry.get("address", "")
            if not wallet_addr:
                continue

            addr_lower = wallet_addr.lower()

            # Jangan override manual atau blacklist entries
            existing = self._wallets.get(addr_lower)
            if existing and existing.source in ("manual", "blacklist"):
                log.debug("nansen_skip_manual_blacklist", wallet=addr_lower)
                continue

            # Hanya proses wallet dengan netflow positif (net buyers)
            netflow_24h = float(entry.get("netflow_24h", 0.0))
            if netflow_24h <= 0:
                continue

            # Tentukan tier dari Nansen label
            raw_label: str = str(entry.get("label", "")).lower().replace(" ", "_")
            tier: Tier = _NANSEN_LABEL_MAP.get(raw_label, "NANSEN_SM_ALL_TIME")

            self._wallets[addr_lower] = SmartWallet(
                address=wallet_addr,
                tier=tier,
                chain="sol",
                source="auto",
                notes=f"nansen:{raw_label}",
                last_updated=datetime.now(timezone.utc).isoformat(),
            )
            tier_counts[tier] = tier_counts.get(tier, 0) + 1

        await self.save()
        log.info("nansen_enrich_done", tier_counts=tier_counts)
        return tier_counts

    def _update_wallet_from_stats(self, wallet: str, stats: dict, chain: str) -> None:
        winrate = float(stats.get("winrate", 0))
        realized = float(stats.get("realized_profit", 0))
        buy_count = int(stats.get("buy_count", 0))
        sell_count = int(stats.get("sell_count", 0))

        # Skip update kalau ini manual/blacklist (preserve override)
        existing = self._wallets.get(wallet.lower())
        if existing and existing.source in ("manual", "blacklist"):
            return

        tier = _classify_tier_from_stats(
            winrate=winrate,
            realized_profit=realized,
            min_trades=self.min_trades,
            buy_count=buy_count,
            sell_count=sell_count,
        )

        self._wallets[wallet.lower()] = SmartWallet(
            address=wallet,
            tier=tier,
            chain=chain,
            winrate=winrate,
            realized_profit=realized,
            total_profit=float(stats.get("total_profit", 0)),
            buy_count=buy_count,
            sell_count=sell_count,
            token_num=int(stats.get("token_num", 0)),
            source="auto",
        )

    def _tier_counts(self) -> dict[Tier, int]:
        counts: dict[Tier, int] = {}
        for sw in self._wallets.values():
            counts[sw.tier] = counts.get(sw.tier, 0) + 1
        return counts

    # ------------------------------------------------------------------
    # Manual operations
    # ------------------------------------------------------------------
    def add_manual(
        self,
        address: str,
        tier: Literal["A", "B"] = "A",
        chain: str = "sol",
        notes: str = "",
    ) -> None:
        """
        Add wallet manually. Akan persist ke `data/smart_wallets_manual.json`.

        Override automatic classification — manual selalu menang.
        """
        manual_data = []
        if self.manual_path.exists():
            try:
                manual_data = json.loads(self.manual_path.read_text())
            except json.JSONDecodeError:
                pass

        # Replace kalau sudah ada
        manual_data = [m for m in manual_data if m.get("address", "").lower() != address.lower()]
        manual_data.append({
            "address": address,
            "tier": tier,
            "chain": chain,
            "notes": notes,
            "added_at": datetime.now(timezone.utc).isoformat(),
        })

        self.manual_path.parent.mkdir(parents=True, exist_ok=True)
        self.manual_path.write_text(json.dumps(manual_data, indent=2))

        # Apply ke in-memory state
        manual_tier: Tier = "MANUAL_A" if tier == "A" else "MANUAL_B"
        self._wallets[address.lower()] = SmartWallet(
            address=address,
            tier=manual_tier,
            chain=chain,
            source="manual",
            notes=notes,
        )
        log.info("wallet_added_manual", address=address, tier=tier, notes=notes)

    def add_blacklist(self, address: str, notes: str = "") -> None:
        """Mark wallet as blacklisted — exclude dari semua scoring."""
        bl_data = []
        if self.blacklist_path.exists():
            try:
                bl_data = json.loads(self.blacklist_path.read_text())
            except json.JSONDecodeError:
                pass

        bl_data = [b for b in bl_data if (b if isinstance(b, str) else b.get("address", "")).lower() != address.lower()]
        bl_data.append({
            "address": address,
            "notes": notes,
            "added_at": datetime.now(timezone.utc).isoformat(),
        })

        self.blacklist_path.parent.mkdir(parents=True, exist_ok=True)
        self.blacklist_path.write_text(json.dumps(bl_data, indent=2))

        if address.lower() in self._wallets:
            self._wallets[address.lower()].tier = "BLACKLIST"
            self._wallets[address.lower()].source = "blacklist"
        log.info("wallet_blacklisted", address=address, notes=notes)

    # ------------------------------------------------------------------
    # Queries (untuk scoring engine + WS subscriber)
    # ------------------------------------------------------------------
    def get_active_wallets(self) -> list[SmartWallet]:
        """A + B + C tier yang siap dipakai untuk scoring."""
        return [w for w in self._wallets.values() if w.is_active]

    def get_top_tier_wallets(self, max_count: int | None = None) -> list[SmartWallet]:
        """
        A + B tier (auto + manual) untuk Helius WebSocket subscribe.

        Default cap = 100 (Helius free tier 5 connections × WebSocket data quota).
        Sort by tier (A first) lalu winrate desc.
        """
        top = [w for w in self._wallets.values() if w.is_top_tier]
        # Sort: NANSEN_FUND → A/MANUAL_A → NANSEN_SM_HOT_30D → NANSEN_SM_ALL_TIME
        #       → NANSEN_SM_HOT_90D → B/MANUAL_B → lainnya, lalu winrate desc
        _tier_rank: dict[str, int] = {
            "NANSEN_FUND": 0,
            "A": 1,
            "MANUAL_A": 1,
            "NANSEN_SM_HOT_30D": 2,
            "NANSEN_SM_ALL_TIME": 3,
            "NANSEN_SM_HOT_90D": 3,
            "B": 4,
            "MANUAL_B": 4,
        }
        top.sort(
            key=lambda w: (
                _tier_rank.get(w.tier, 99),
                -w.winrate,
            )
        )
        if max_count:
            return top[:max_count]
        return top

    def get_by_address(self, address: str) -> SmartWallet | None:
        return self._wallets.get(address.lower())

    def is_smart_wallet(self, address: str) -> bool:
        sw = self._wallets.get(address.lower())
        return sw is not None and sw.is_active

    def stats_summary(self) -> dict[str, int]:
        """Quick visibility — total per tier."""
        return self._tier_counts()


# Module-level singleton — opsional, user bisa instantiate sendiri
_global_registry: SmartWalletRegistry | None = None


def get_global_registry() -> SmartWalletRegistry:
    global _global_registry
    if _global_registry is None:
        _global_registry = SmartWalletRegistry()
    return _global_registry


# CLI helper untuk test/refresh manual
async def main() -> None:
    """Bootstrap registry dari nol. Jalankan:

        python -m src.core.smart_wallet_registry
    """
    if not settings.gmgn_api_key:
        print("ERROR: GMGN_API_KEY belum set. Lengkapi secrets/.env dulu.")
        return

    registry = SmartWalletRegistry()
    await registry.load()
    print(f"Loaded existing: {registry.stats_summary()}")

    print("\nBootstrapping dari GMGN (~60 detik untuk 200 candidates)...")
    async with GMGNClient() as gmgn:
        result = await registry.bootstrap_from_gmgn(gmgn, sample_size=200)

    print("\nResult:")
    for tier, count in result.items():
        print(f"  {tier}: {count}")

    top = registry.get_top_tier_wallets(max_count=10)
    print(f"\nTop 10 (A+B tier):")
    for sw in top:
        print(f"  {sw.tier} | winrate={sw.winrate:.1%} | profit={sw.realized_profit:.1f} | {sw.address}")


if __name__ == "__main__":
    asyncio.run(main())
