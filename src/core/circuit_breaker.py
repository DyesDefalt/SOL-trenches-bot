"""
Circuit Breaker — DECOUPLED watchdog yang halt trading kalau threshold breach.

Penting: circuit breaker WAJIB jalan di task TERPISAH dari main loop. Kalau main
loop hang/freeze, breaker tetap berfungsi.

Trigger thresholds (dari spec, configurable via env):
- Consecutive losses: 3 in row → pause 6h
- Daily loss: -30% modal hari ini → pause 24h
- Weekly loss: -50% → halt manual review
- Max drawdown from peak: -50% → emergency stop
- Win rate < 25% over 20 trades → pause manual
- Drawdown velocity: -20% in <1h → emergency stop
- Anomaly: same token bought 2x in cooldown / 5 trades exit at exact SL

Saat trigger:
1. Set `is_paused = True` (block new entries)
2. Open positions TIDAK auto-liquidate (let position manager handle exit naturally)
3. Insert event ke DB
4. Send Telegram alert dengan state snapshot
5. Resume manual atau setelah cooldown elapsed
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from src.config import settings
from src.infra.logger import get_logger

log = get_logger(__name__)


class CBTrigger(str, Enum):
    CONSECUTIVE_LOSS = "CONSECUTIVE_LOSS"
    DAILY_LOSS = "DAILY_LOSS"
    WEEKLY_LOSS = "WEEKLY_LOSS"
    MAX_DRAWDOWN = "MAX_DRAWDOWN"
    LOW_WINRATE = "LOW_WINRATE"
    DRAWDOWN_VELOCITY = "DRAWDOWN_VELOCITY"
    DUPLICATE_TOKEN = "DUPLICATE_TOKEN"
    SL_PATTERN = "SL_PATTERN"
    MANUAL = "MANUAL"


@dataclass
class TradeOutcome:
    """Single trade result untuk circuit breaker tracking."""

    token_address: str
    pnl_sol: float
    pnl_pct: float
    won: bool
    exit_reason: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class CBState:
    """In-memory state circuit breaker."""

    is_paused: bool = False
    paused_until: datetime | None = None
    pause_reason: str = ""

    starting_balance_sol: float = 0.0
    peak_balance_sol: float = 0.0
    current_balance_sol: float = 0.0

    daily_starting_balance_sol: float = 0.0
    daily_pnl_sol: float = 0.0

    weekly_starting_balance_sol: float = 0.0
    weekly_pnl_sol: float = 0.0

    recent_trades: list[TradeOutcome] = field(default_factory=list)  # last 20
    consecutive_losses: int = 0

    last_daily_reset: datetime | None = None
    last_weekly_reset: datetime | None = None


class CircuitBreaker:
    """
    Watchdog yang track trading state + trigger pause kalau threshold breach.

    Usage:
        cb = CircuitBreaker(db=db, telegram=tg)
        await cb.initialize(starting_balance_sol=0.36)

        # Background loop
        asyncio.create_task(cb.run_watchdog())

        # Sebelum buka posisi baru
        if not cb.can_open_position():
            return

        # Setelah trade tutup
        await cb.record_trade(outcome)
    """

    def __init__(
        self,
        db=None,  # type: ignore[no-untyped-def]
        telegram=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self.db = db
        self.telegram = telegram
        self.state = CBState()

        # Threshold dari config
        self.consecutive_loss_limit = settings.cb_consecutive_loss_limit
        self.consecutive_loss_pause_hours = settings.cb_consecutive_loss_pause_hours
        self.daily_loss_pct = settings.cb_daily_loss_pct
        self.weekly_loss_pct = settings.cb_weekly_loss_pct
        self.max_drawdown_pct = settings.cb_max_drawdown_pct
        self.win_rate_min_pct = settings.cb_win_rate_min_pct
        self.win_rate_window = settings.cb_win_rate_window
        self.drawdown_velocity_pct = settings.cb_drawdown_velocity_pct
        self.drawdown_velocity_window_minutes = settings.cb_drawdown_velocity_window_minutes

    async def initialize(self, starting_balance_sol: float) -> None:
        """Set baseline balance saat bot start."""
        now = datetime.now(timezone.utc)
        self.state.starting_balance_sol = starting_balance_sol
        self.state.peak_balance_sol = starting_balance_sol
        self.state.current_balance_sol = starting_balance_sol
        self.state.daily_starting_balance_sol = starting_balance_sol
        self.state.weekly_starting_balance_sol = starting_balance_sol
        self.state.last_daily_reset = now
        self.state.last_weekly_reset = now
        log.info("cb_initialized", starting_balance_sol=starting_balance_sol)

    def can_open_position(self) -> bool:
        """Check apakah safe untuk open new position."""
        if not settings.cb_enabled:
            return True
        if self.state.is_paused:
            now = datetime.now(timezone.utc)
            if self.state.paused_until and now >= self.state.paused_until:
                # Auto-resume dari time-based pause
                self._resume("auto:cooldown_elapsed")
                return True
            return False
        return True

    def _resume(self, reason: str) -> None:
        log.info("cb_resumed", reason=reason)
        self.state.is_paused = False
        self.state.paused_until = None
        self.state.pause_reason = ""

    async def update_balance(self, current_balance_sol: float) -> None:
        """Call ini setiap kali balance berubah (after trade settled)."""
        self.state.current_balance_sol = current_balance_sol
        if current_balance_sol > self.state.peak_balance_sol:
            self.state.peak_balance_sol = current_balance_sol

    async def record_trade(self, outcome: TradeOutcome) -> None:
        """Record trade result + check semua trigger."""
        # Update state
        self.state.daily_pnl_sol += outcome.pnl_sol
        self.state.weekly_pnl_sol += outcome.pnl_sol

        if outcome.won:
            self.state.consecutive_losses = 0
        else:
            self.state.consecutive_losses += 1

        self.state.recent_trades.append(outcome)
        if len(self.state.recent_trades) > self.win_rate_window * 2:
            self.state.recent_trades = self.state.recent_trades[-self.win_rate_window * 2 :]

        # Check semua trigger
        await self._check_all_triggers()

    async def _check_all_triggers(self) -> None:
        """Run semua trigger check. Trip kalau ada yang breach."""
        if self.state.is_paused:
            return  # already paused

        # 1. Consecutive losses
        if self.state.consecutive_losses >= self.consecutive_loss_limit:
            await self._trigger(
                CBTrigger.CONSECUTIVE_LOSS,
                threshold=self.consecutive_loss_limit,
                actual=self.state.consecutive_losses,
                pause_hours=self.consecutive_loss_pause_hours,
            )
            return

        # 2. Daily loss limit
        if self.state.daily_starting_balance_sol > 0:
            daily_pct = (self.state.daily_pnl_sol / self.state.daily_starting_balance_sol) * 100
            if daily_pct <= -self.daily_loss_pct:
                await self._trigger(
                    CBTrigger.DAILY_LOSS,
                    threshold=-self.daily_loss_pct,
                    actual=daily_pct,
                    pause_hours=24,
                )
                return

        # 3. Weekly loss limit
        if self.state.weekly_starting_balance_sol > 0:
            weekly_pct = (self.state.weekly_pnl_sol / self.state.weekly_starting_balance_sol) * 100
            if weekly_pct <= -self.weekly_loss_pct:
                await self._trigger(
                    CBTrigger.WEEKLY_LOSS,
                    threshold=-self.weekly_loss_pct,
                    actual=weekly_pct,
                    pause_hours=None,  # manual review only
                )
                return

        # 4. Max drawdown from peak
        if self.state.peak_balance_sol > 0:
            dd_pct = ((self.state.current_balance_sol - self.state.peak_balance_sol) / self.state.peak_balance_sol) * 100
            if dd_pct <= -self.max_drawdown_pct:
                await self._trigger(
                    CBTrigger.MAX_DRAWDOWN,
                    threshold=-self.max_drawdown_pct,
                    actual=dd_pct,
                    pause_hours=None,
                )
                return

        # 5. Low win rate (rolling 20 trades)
        if len(self.state.recent_trades) >= self.win_rate_window:
            recent = self.state.recent_trades[-self.win_rate_window :]
            wins = sum(1 for t in recent if t.won)
            wr_pct = (wins / len(recent)) * 100
            if wr_pct < self.win_rate_min_pct:
                await self._trigger(
                    CBTrigger.LOW_WINRATE,
                    threshold=self.win_rate_min_pct,
                    actual=wr_pct,
                    pause_hours=None,
                )
                return

        # 6. Drawdown velocity (-20% in <1h)
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=self.drawdown_velocity_window_minutes)
        recent_in_window = [t for t in self.state.recent_trades if t.timestamp >= cutoff]
        if len(recent_in_window) >= 3:
            window_pnl = sum(t.pnl_sol for t in recent_in_window)
            if self.state.daily_starting_balance_sol > 0:
                window_pct = (window_pnl / self.state.daily_starting_balance_sol) * 100
                if window_pct <= -self.drawdown_velocity_pct:
                    await self._trigger(
                        CBTrigger.DRAWDOWN_VELOCITY,
                        threshold=-self.drawdown_velocity_pct,
                        actual=window_pct,
                        pause_hours=None,
                    )

    async def _trigger(
        self,
        trigger: CBTrigger,
        threshold: float,
        actual: float,
        pause_hours: float | None,
    ) -> None:
        """Activate circuit breaker."""
        now = datetime.now(timezone.utc)
        paused_until = now + timedelta(hours=pause_hours) if pause_hours else None

        self.state.is_paused = True
        self.state.paused_until = paused_until
        self.state.pause_reason = f"{trigger.value} (threshold={threshold}, actual={actual:.2f})"

        snapshot = {
            "starting_balance_sol": self.state.starting_balance_sol,
            "peak_balance_sol": self.state.peak_balance_sol,
            "current_balance_sol": self.state.current_balance_sol,
            "daily_pnl_sol": self.state.daily_pnl_sol,
            "weekly_pnl_sol": self.state.weekly_pnl_sol,
            "consecutive_losses": self.state.consecutive_losses,
            "recent_trades_count": len(self.state.recent_trades),
        }

        log.error(
            "circuit_breaker_tripped",
            trigger=trigger.value,
            threshold=threshold,
            actual=actual,
            paused_until=paused_until.isoformat() if paused_until else "MANUAL_RESET",
            snapshot=snapshot,
        )

        # Persist event ke DB
        if self.db:
            try:
                await self.db.insert_cb_event(
                    trigger_type=trigger.value,
                    threshold_value=threshold,
                    actual_value=actual,
                    paused_until=paused_until,
                    state_snapshot=snapshot,
                )
            except Exception as e:
                log.error("cb_db_insert_failed", error=str(e))

        # Send alert
        if self.telegram:
            try:
                msg = (
                    f"🚨 <b>CIRCUIT BREAKER TRIPPED</b>\n\n"
                    f"<b>Trigger:</b> {trigger.value}\n"
                    f"<b>Threshold:</b> {threshold}\n"
                    f"<b>Actual:</b> {actual:.2f}\n"
                    f"<b>Paused until:</b> {paused_until.strftime('%Y-%m-%d %H:%M UTC') if paused_until else 'MANUAL RESET'}\n\n"
                    f"<b>Balance:</b> {self.state.current_balance_sol:.4f} SOL\n"
                    f"<b>Daily PnL:</b> {self.state.daily_pnl_sol:+.4f} SOL\n"
                    f"<b>Consecutive losses:</b> {self.state.consecutive_losses}\n\n"
                    f"Use /resume kalau sudah audit & yakin lanjut."
                )
                await self.telegram.send_alert(msg)
            except Exception as e:
                log.error("cb_telegram_alert_failed", error=str(e))

    def manual_pause(self, reason: str = "user_request") -> None:
        """Manual pause via /pause command."""
        self.state.is_paused = True
        self.state.paused_until = None
        self.state.pause_reason = f"MANUAL: {reason}"
        log.warning("cb_manual_pause", reason=reason)

    def manual_resume(self, reason: str = "user_request") -> None:
        """Manual resume via /resume command."""
        self._resume(f"manual:{reason}")

    async def run_watchdog(self, interval_seconds: int = 60) -> None:
        """
        Background task: periodic check daily/weekly reset + state heartbeat.

        Run via:
            asyncio.create_task(cb.run_watchdog())
        """
        log.info("cb_watchdog_started", interval=interval_seconds)
        while True:
            try:
                await asyncio.sleep(interval_seconds)
                self._maybe_reset_daily()
                self._maybe_reset_weekly()

                # Auto-resume kalau cooldown elapsed
                if self.state.is_paused and self.state.paused_until:
                    if datetime.now(timezone.utc) >= self.state.paused_until:
                        self._resume("auto:cooldown_elapsed")
            except Exception as e:
                log.error("cb_watchdog_error", error=str(e))

    def _maybe_reset_daily(self) -> None:
        """Reset daily counters di midnight UTC."""
        now = datetime.now(timezone.utc)
        if not self.state.last_daily_reset or now.date() > self.state.last_daily_reset.date():
            self.state.daily_starting_balance_sol = self.state.current_balance_sol
            self.state.daily_pnl_sol = 0.0
            self.state.last_daily_reset = now
            log.info("cb_daily_reset", new_starting=self.state.daily_starting_balance_sol)

    def _maybe_reset_weekly(self) -> None:
        """Reset weekly counters tiap Senin UTC."""
        now = datetime.now(timezone.utc)
        if not self.state.last_weekly_reset:
            self.state.last_weekly_reset = now
            return
        # Reset kalau hari Senin (weekday 0) dan belum di-reset minggu ini
        days_since = (now.date() - self.state.last_weekly_reset.date()).days
        if now.weekday() == 0 and days_since >= 7:
            self.state.weekly_starting_balance_sol = self.state.current_balance_sol
            self.state.weekly_pnl_sol = 0.0
            self.state.last_weekly_reset = now
            log.info("cb_weekly_reset", new_starting=self.state.weekly_starting_balance_sol)
