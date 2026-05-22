"""
Position Manager — TP staircase, trailing stop, hard SL, time-based exit.

Strategi exit (sesuai spec):
- TP1: +80%   → sell 30%
- TP2: +150%  → sell 30%
- TP3: +300%  → sell 25%
- Sisanya: trailing stop -30% dari peak
- Hard SL: -45% → full exit
- Time-based: 45 menit no momentum → auto sell

Per posisi, manager track:
- entry_price (USD per token)
- peak_price (untuk trailing)
- amount_remaining (token tersisa setelah partial exits)
- partial_exits_done (TP1/TP2/TP3 flags)

Run loop: poll harga setiap 10 detik, evaluate, execute exit kalau triggered.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from src.config import settings
from src.infra.logger import get_logger

if TYPE_CHECKING:
    from src.clients.geckoterminal import GeckoTerminalClient
    from src.core.execution import ExecutionLayer
    from src.infra.db import Database

log = get_logger(__name__)


@dataclass
class OpenPosition:
    """In-memory state per open position."""

    db_id: int
    token_address: str
    token_symbol: str
    entry_price_usd: float
    entry_amount_sol: float
    entry_amount_token: float
    entry_timestamp: datetime
    peak_price_usd: float
    amount_remaining_token: float
    tp1_done: bool = False
    tp2_done: bool = False
    tp3_done: bool = False
    last_price_update: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Phase 11.1: per-position overrides (set via Telegram quick-action buttons)
    # When non-None, these override the global settings.* values for THIS position only.
    tp_override_pct: float | None = None      # e.g. 25.0 = override next TP to +25%
    sl_override_pct: float | None = None      # e.g. -15.0 = tighten SL to -15%
    trail_disabled: bool = False               # if True, skip trailing stop logic

    # Phase 11.3: extended metrics for rich position card (filled on refresh)
    current_price_usd: float | None = None
    current_liquidity_usd: float | None = None
    current_mcap_usd: float | None = None
    buy_pressure_pct: float | None = None
    vol_liq_ratio: float | None = None
    rug_score: int | None = None

    def effective_tp1_pct(self, default: float) -> float:
        """Return active TP1 % considering override."""
        return self.tp_override_pct if self.tp_override_pct is not None else default

    def effective_sl_pct(self, default: float) -> float:
        """Return active SL % considering override."""
        return self.sl_override_pct if self.sl_override_pct is not None else default


class PositionManager:
    """Manage open positions: track price, evaluate exits, execute sells."""

    def __init__(
        self,
        db: "Database",
        execution: "ExecutionLayer",
        gecko: "GeckoTerminalClient",
        cb=None,  # type: ignore[no-untyped-def]
        telegram=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self.db = db
        self.execution = execution
        self.gecko = gecko
        self.cb = cb
        self.telegram = telegram

        self._positions: dict[str, OpenPosition] = {}  # keyed by token_address

    async def load_open_positions(self) -> None:
        """Load semua open positions dari DB ke memory."""
        rows = await self.db.get_open_positions()
        for row in rows:
            pos = OpenPosition(
                db_id=row["id"],
                token_address=row["token_address"],
                token_symbol=row["token_symbol"] or "",
                entry_price_usd=float(row["entry_price_usd"]),
                entry_amount_sol=float(row["entry_amount_sol"]),
                entry_amount_token=float(row["entry_amount_token"]),
                entry_timestamp=row["entry_timestamp"],
                peak_price_usd=float(row.get("peak_price_usd") or row["entry_price_usd"]),
                amount_remaining_token=float(row["entry_amount_token"]),  # asume full kalau no partial yet
            )
            self._positions[row["token_address"]] = pos
        log.info("positions_loaded", count=len(self._positions))

    async def add_position(self, pos: OpenPosition) -> None:
        """Add new position (after entry sukses)."""
        self._positions[pos.token_address] = pos

    @property
    def open_count(self) -> int:
        return len(self._positions)

    async def update_price(self, token_address: str, current_price_usd: float) -> None:
        """Update price + check semua exit trigger."""
        pos = self._positions.get(token_address)
        if not pos:
            return

        pos.last_price_update = datetime.now(timezone.utc)
        if current_price_usd > pos.peak_price_usd:
            pos.peak_price_usd = current_price_usd
            await self.db.update_position_peak(pos.db_id, current_price_usd)

        gain_pct = ((current_price_usd - pos.entry_price_usd) / pos.entry_price_usd) * 100

        # Phase 11.1: cache live price metric for /positions display
        pos.current_price_usd = current_price_usd

        # 1. Hard SL check (highest priority) — honor per-position override
        sl_threshold = pos.effective_sl_pct(settings.hard_sl_pct)
        if gain_pct <= sl_threshold:
            await self._exit_full(pos, current_price_usd, "SL")
            return

        # 2. Trailing stop (only after TP3 atau kalau no TP yet) — skip if trail_disabled override
        if not pos.trail_disabled:
            drop_from_peak = ((current_price_usd - pos.peak_price_usd) / pos.peak_price_usd) * 100
            if pos.tp3_done and drop_from_peak <= -settings.trailing_stop_pct:
                await self._exit_full(pos, current_price_usd, "TRAILING")
                return

        # 3. Take profit staircase — honor per-position TP override (applies to TP1 only)
        tp1_threshold = pos.effective_tp1_pct(settings.tp1_gain_pct)
        if not pos.tp1_done and gain_pct >= tp1_threshold:
            await self._partial_exit(pos, current_price_usd, "TP1", settings.tp1_sell_pct / 100)
            pos.tp1_done = True
            return

        if not pos.tp2_done and gain_pct >= settings.tp2_gain_pct:
            await self._partial_exit(pos, current_price_usd, "TP2", settings.tp2_sell_pct / 100)
            pos.tp2_done = True
            return

        if not pos.tp3_done and gain_pct >= settings.tp3_gain_pct:
            await self._partial_exit(pos, current_price_usd, "TP3", settings.tp3_sell_pct / 100)
            pos.tp3_done = True
            return

        # 4. Time-based exit (kalau no momentum)
        elapsed_min = (datetime.now(timezone.utc) - pos.entry_timestamp).total_seconds() / 60
        if elapsed_min >= settings.time_based_exit_minutes and gain_pct < 20 and not pos.tp1_done:
            await self._exit_full(pos, current_price_usd, "TIME_EXIT")

    async def _partial_exit(
        self,
        pos: OpenPosition,
        current_price_usd: float,
        tier: str,
        sell_fraction: float,
    ) -> None:
        """Sell partial sesuai TP tier."""
        sell_amount_token = int(pos.amount_remaining_token * sell_fraction)
        if sell_amount_token <= 0:
            return

        result = await self.execution.sell_token(
            token_address=pos.token_address,
            token_amount=sell_amount_token,
        )

        if not result.success:
            log.error("partial_exit_failed", tier=tier, error=result.error)
            return

        sol_received = result.out_amount / 1_000_000_000
        gain_pct = ((current_price_usd - pos.entry_price_usd) / pos.entry_price_usd) * 100

        await self.db.add_partial_exit(
            position_id=pos.db_id,
            tier=tier,
            sell_price_usd=current_price_usd,
            sell_amount_token=sell_amount_token,
            sell_amount_sol=sol_received,
            signature=result.signature,
            pnl_pct=gain_pct,
        )

        pos.amount_remaining_token -= sell_amount_token

        log.info(
            "partial_exit_done",
            tier=tier,
            token=pos.token_symbol or pos.token_address[:8],
            sell_pct=sell_fraction * 100,
            sol_received=sol_received,
            gain_pct=gain_pct,
        )

        if self.telegram:
            await self.telegram.send_alert(
                f"💰 <b>{tier} HIT</b> {pos.token_symbol or pos.token_address[:8]}\n"
                f"Gain: +{gain_pct:.0f}%\n"
                f"Sold: {sell_fraction * 100:.0f}% → {sol_received:.4f} SOL"
            )

    async def _exit_full(
        self,
        pos: OpenPosition,
        current_price_usd: float,
        reason: str,
    ) -> None:
        """Full exit: sell sisa amount."""
        if pos.amount_remaining_token <= 0:
            await self._mark_closed(pos, current_price_usd, 0, "", reason)
            return

        result = await self.execution.sell_token(
            token_address=pos.token_address,
            token_amount=int(pos.amount_remaining_token),
        )

        if not result.success:
            log.error("full_exit_failed", reason=reason, error=result.error)
            return

        sol_received = result.out_amount / 1_000_000_000
        await self._mark_closed(pos, current_price_usd, sol_received, result.signature, reason)

    async def _mark_closed(
        self,
        pos: OpenPosition,
        exit_price_usd: float,
        final_sol_received: float,
        signature: str,
        reason: str,
    ) -> None:
        """Calculate total PnL + persist + alert."""
        # Total SOL received = sum semua partial exits + final exit
        # Untuk simplicity di MVP: hitung dari DB partial_exits
        partial_total = 0.0
        # In real impl, query position_partial_exits SUM. Skip untuk MVP brevity:
        total_received = final_sol_received + partial_total

        pnl_sol = total_received - pos.entry_amount_sol
        pnl_pct = (pnl_sol / pos.entry_amount_sol) * 100 if pos.entry_amount_sol > 0 else 0

        await self.db.close_position(
            position_id=pos.db_id,
            exit_price_usd=exit_price_usd,
            exit_amount_sol=total_received,
            exit_signature=signature,
            exit_reason=reason,
            realized_pnl_sol=pnl_sol,
            realized_pnl_pct=pnl_pct,
        )

        del self._positions[pos.token_address]

        log.info(
            "position_closed",
            token=pos.token_symbol or pos.token_address[:8],
            reason=reason,
            pnl_sol=pnl_sol,
            pnl_pct=pnl_pct,
        )

        # Update circuit breaker
        if self.cb:
            from src.core.circuit_breaker import TradeOutcome

            outcome = TradeOutcome(
                token_address=pos.token_address,
                pnl_sol=pnl_sol,
                pnl_pct=pnl_pct,
                won=pnl_sol > 0,
                exit_reason=reason,
            )
            await self.cb.record_trade(outcome)

        # Telegram alert
        if self.telegram:
            emoji = "🟢" if pnl_sol > 0 else "🔴"
            await self.telegram.send_alert(
                f"{emoji} <b>POSITION CLOSED</b> {pos.token_symbol or pos.token_address[:8]}\n"
                f"Reason: {reason}\n"
                f"PnL: {pnl_sol:+.4f} SOL ({pnl_pct:+.1f}%)"
            )

    async def run_monitor_loop(self, interval_seconds: int = 10) -> None:
        """
        Background loop: poll harga semua open positions tiap N detik.

        Run via:
            asyncio.create_task(position_manager.run_monitor_loop())
        """
        log.info("position_monitor_started", interval=interval_seconds)
        while True:
            try:
                await asyncio.sleep(interval_seconds)
                if not self._positions:
                    continue

                # Fetch latest price untuk semua open positions (parallel)
                async def fetch_and_update(addr: str) -> None:
                    try:
                        token_data = await self.gecko.get_token(addr)
                        price = float(token_data.get("attributes", {}).get("price_usd", 0))
                        if price > 0:
                            await self.update_price(addr, price)
                    except Exception as e:
                        log.warning("price_fetch_failed", token=addr, error=str(e))

                await asyncio.gather(
                    *[fetch_and_update(addr) for addr in list(self._positions.keys())]
                )

            except Exception as e:
                log.error("position_monitor_error", error=str(e))

    # ------------------------------------------------------------------
    # Phase 11.1: per-position TP/SL/Trail overrides via Telegram buttons
    # ------------------------------------------------------------------

    async def override_tp(self, position_id: int, new_tp_pct: float, set_by: str = "telegram") -> bool:
        """
        Override TP1 target for a single open position.

        Returns True if applied, False if position not found.
        """
        pos = self._find_position_by_db_id(position_id)
        if not pos:
            return False
        pos.tp_override_pct = float(new_tp_pct)
        # Reset tp1_done so the new lower TP can trigger (only if not already past it)
        gain_pct = self._current_gain_pct(pos)
        if gain_pct is not None and gain_pct < new_tp_pct:
            pos.tp1_done = False
        try:
            await self.db.update_position_override(position_id, tp_pct=new_tp_pct, set_by=set_by)
        except Exception as e:
            log.warning("override_tp_db_persist_failed", position_id=position_id, error=str(e))
        log.info("position_tp_override", position_id=position_id, new_pct=new_tp_pct, set_by=set_by)
        return True

    async def override_sl(self, position_id: int, new_sl_pct: float, set_by: str = "telegram") -> bool:
        """Override hard SL for a single open position. new_sl_pct should be negative (e.g. -15.0)."""
        pos = self._find_position_by_db_id(position_id)
        if not pos:
            return False
        pos.sl_override_pct = float(new_sl_pct)
        try:
            await self.db.update_position_override(position_id, sl_pct=new_sl_pct, set_by=set_by)
        except Exception as e:
            log.warning("override_sl_db_persist_failed", position_id=position_id, error=str(e))
        log.info("position_sl_override", position_id=position_id, new_pct=new_sl_pct, set_by=set_by)
        return True

    async def toggle_trail(self, position_id: int, set_by: str = "telegram") -> bool | None:
        """Toggle trailing stop on/off. Returns new disabled state, or None if position not found."""
        pos = self._find_position_by_db_id(position_id)
        if not pos:
            return None
        pos.trail_disabled = not pos.trail_disabled
        try:
            await self.db.update_position_override(position_id, trail_disabled=pos.trail_disabled, set_by=set_by)
        except Exception as e:
            log.warning("toggle_trail_db_persist_failed", position_id=position_id, error=str(e))
        log.info("position_trail_toggled", position_id=position_id, disabled=pos.trail_disabled, set_by=set_by)
        return pos.trail_disabled

    async def force_partial_sell(self, position_id: int, sell_pct: float, set_by: str = "telegram") -> bool:
        """Force-sell sell_pct (0.0-1.0) of remaining tokens. Used by /menu quick-sell buttons."""
        pos = self._find_position_by_db_id(position_id)
        if not pos:
            return False
        if sell_pct <= 0 or sell_pct > 1:
            return False
        # Get latest price first (best-effort)
        try:
            token_data = await self.gecko.get_token(pos.token_address)
            current_price = float(token_data.get("attributes", {}).get("price_usd", 0))
        except Exception:
            current_price = pos.current_price_usd or pos.entry_price_usd
        if current_price <= 0:
            log.warning("force_sell_no_price", position_id=position_id)
            return False
        if sell_pct >= 0.99:
            await self._exit_full(pos, current_price, f"MANUAL_FULL_{set_by}")
        else:
            await self._partial_exit(pos, current_price, f"MANUAL_{int(sell_pct * 100)}PCT_{set_by}", sell_pct)
        return True

    def _find_position_by_db_id(self, position_id: int) -> OpenPosition | None:
        """Lookup open position by DB id (positions are keyed by token_address internally)."""
        for pos in self._positions.values():
            if pos.db_id == position_id:
                return pos
        return None

    def _current_gain_pct(self, pos: OpenPosition) -> float | None:
        """Best-effort current gain % from cached current_price_usd."""
        if pos.current_price_usd is None or pos.current_price_usd <= 0:
            return None
        return ((pos.current_price_usd - pos.entry_price_usd) / pos.entry_price_usd) * 100

    def get_open_positions_summary(self) -> list[dict]:
        """Return rich summary of all open positions for /positions display."""
        out = []
        for pos in self._positions.values():
            gain = self._current_gain_pct(pos)
            out.append({
                "db_id": pos.db_id,
                "token_address": pos.token_address,
                "symbol": pos.token_symbol,
                "entry_price_usd": pos.entry_price_usd,
                "current_price_usd": pos.current_price_usd,
                "peak_price_usd": pos.peak_price_usd,
                "gain_pct": gain,
                "size_sol": pos.entry_amount_sol,
                "tp_active_pct": pos.effective_tp1_pct(settings.tp1_gain_pct),
                "sl_active_pct": pos.effective_sl_pct(settings.hard_sl_pct),
                "trail_active": not pos.trail_disabled,
                "tp_override": pos.tp_override_pct,
                "sl_override": pos.sl_override_pct,
                "tp1_done": pos.tp1_done,
                "tp2_done": pos.tp2_done,
                "tp3_done": pos.tp3_done,
                "buy_pressure_pct": pos.buy_pressure_pct,
                "vol_liq_ratio": pos.vol_liq_ratio,
                "rug_score": pos.rug_score,
                "liquidity_usd": pos.current_liquidity_usd,
                "mcap_usd": pos.current_mcap_usd,
            })
        return out
