"""
Telegram Bot Interface — alert + remote command.

Commands:
- /start, /help
- /status        — bot status, posisi aktif, balance
- /pnl           — daily PnL summary
- /pause         — manual pause trading
- /resume        — resume after pause
- /smartlist     — top 10 smart wallets
- /addwallet ADDRESS [tier] [notes]   — manual add
- /blacklist ADDRESS [notes]
- /config        — show current settings
- /force_sell ADDRESS — force exit posisi
- /stats         — full stats

Usage:
    bot = TelegramBot(...)
    await bot.start_polling()    # runs forever
"""

from __future__ import annotations

import asyncio
import html
from typing import TYPE_CHECKING

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from src.config import settings
from src.infra.logger import get_logger

if TYPE_CHECKING:
    from src.core.circuit_breaker import CircuitBreaker
    from src.core.position import PositionManager
    from src.core.smart_wallet_registry import SmartWalletRegistry
    from src.infra.db import Database

log = get_logger(__name__)


class TelegramBot:
    """Telegram interface untuk bot control + alerts."""

    def __init__(
        self,
        registry: "SmartWalletRegistry | None" = None,
        cb: "CircuitBreaker | None" = None,
        position_manager: "PositionManager | None" = None,
        db: "Database | None" = None,
        callback_router: object | None = None,
        strategy_manager: object | None = None,
        price_alert_manager: object | None = None,
    ) -> None:
        self.token = settings.telegram_bot_token
        self.chat_id = settings.telegram_chat_id
        if not self.token or not self.chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not configured")

        self.registry = registry
        self.cb = cb
        self.position_manager = position_manager
        self.db = db
        # Phase 10: optional interactive menu router + strategy/alerts handles
        self.callback_router = callback_router
        self.strategy_manager = strategy_manager
        self.price_alert_manager = price_alert_manager
        self._app: Application | None = None

    def _build_app(self) -> Application:
        app = Application.builder().token(self.token).build()
        app.add_handler(CommandHandler("start", self._handle_start))
        app.add_handler(CommandHandler("help", self._handle_help))
        app.add_handler(CommandHandler("status", self._handle_status))
        app.add_handler(CommandHandler("pnl", self._handle_pnl))
        app.add_handler(CommandHandler("pause", self._handle_pause))
        app.add_handler(CommandHandler("resume", self._handle_resume))
        app.add_handler(CommandHandler("smartlist", self._handle_smartlist))
        app.add_handler(CommandHandler("addwallet", self._handle_addwallet))
        app.add_handler(CommandHandler("blacklist", self._handle_blacklist))
        app.add_handler(CommandHandler("config", self._handle_config))
        app.add_handler(CommandHandler("stats", self._handle_stats))
        # Phase 6b/6c commands
        app.add_handler(CommandHandler("applyTuning", self._handle_apply_tuning))
        app.add_handler(CommandHandler("walletinfo", self._handle_wallet_info))
        # Phase 10: interactive menu commands
        app.add_handler(CommandHandler("menu", self._handle_menu))
        app.add_handler(CommandHandler("strategy", self._handle_strategy))
        app.add_handler(CommandHandler("stratset", self._handle_stratset))
        app.add_handler(CommandHandler("alerts", self._handle_alerts))
        app.add_handler(CommandHandler("feeclaims", self._handle_feeclaims))
        # Phase 10: inline button callbacks (e.g., menu navigation, approve/reject intents)
        if self.callback_router is not None:
            app.add_handler(CallbackQueryHandler(self.callback_router.handle_callback))
        return app

    async def start_polling(self) -> None:
        """Run polling loop (blocks forever)."""
        self._app = self._build_app()
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()
        log.info("telegram_bot_started")

    async def stop(self) -> None:
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()

    async def send_alert(self, message: str) -> None:
        """Send message ke configured chat_id."""
        if not self._app:
            # Lazy fallback: pakai requests langsung kalau bot belum start
            import httpx

            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(
                        url,
                        data={
                            "chat_id": self.chat_id,
                            "text": message,
                            "parse_mode": "HTML",
                        },
                    )
            except Exception as e:
                log.warning("telegram_send_fallback_failed", error=str(e))
            return

        try:
            await self._app.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            log.warning("telegram_send_failed", error=str(e))

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------
    def _is_authorized(self, update: Update) -> bool:
        """Restrict commands ke configured chat_id."""
        return str(update.effective_chat.id) == str(self.chat_id) if update.effective_chat else False

    async def _handle_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        msg = (
            "👋 <b>Solana Sniper Bot</b>\n\n"
            "Bot autonomous untuk Solana memecoin sniping.\n\n"
            "Commands:\n"
            "/status — status bot + posisi aktif\n"
            "/pnl — PnL hari ini\n"
            "/pause — pause trading\n"
            "/resume — lanjut trading\n"
            "/smartlist — top 10 smart wallets\n"
            "/addwallet ADDRESS — manual add wallet\n"
            "/blacklist ADDRESS — blacklist wallet\n"
            "/config — current settings\n"
            "/help — bantuan"
        )
        await update.message.reply_html(msg)

    async def _handle_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._handle_start(update, ctx)

    async def _handle_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return

        lines = ["<b>📊 BOT STATUS</b>"]
        lines.append(f"DRY_RUN: <code>{settings.dry_run}</code>")

        if self.cb:
            cb_state = "🟢 ACTIVE" if not self.cb.state.is_paused else f"🔴 PAUSED ({self.cb.state.pause_reason})"
            lines.append(f"Trading: {cb_state}")
            lines.append(f"Balance: {self.cb.state.current_balance_sol:.4f} SOL")
            lines.append(f"Daily PnL: {self.cb.state.daily_pnl_sol:+.4f} SOL")
            lines.append(f"Consecutive losses: {self.cb.state.consecutive_losses}")

        if self.position_manager:
            lines.append(f"Open positions: {self.position_manager.open_count}")

        if self.registry:
            stats = self.registry.stats_summary()
            top_count = stats.get("A", 0) + stats.get("B", 0) + stats.get("MANUAL_A", 0) + stats.get("MANUAL_B", 0)
            lines.append(f"Smart wallets (A+B): {top_count}")

        await update.message.reply_html("\n".join(lines))

    async def _handle_pnl(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return

        if not self.db:
            await update.message.reply_text("DB tidak tersedia.")
            return

        days = await self.db.get_daily_pnl(days=7)
        if not days:
            await update.message.reply_text("Belum ada data PnL.")
            return

        lines = ["<b>📈 PNL (7 days)</b>"]
        for d in days:
            emoji = "🟢" if float(d["pnl_sol"]) > 0 else "🔴" if float(d["pnl_sol"]) < 0 else "⚪"
            lines.append(
                f"{emoji} {d['date']}: {float(d['pnl_sol']):+.4f} SOL "
                f"({d['trades_total']}t, {d['trades_won']}W/{d['trades_lost']}L)"
            )
        await update.message.reply_html("\n".join(lines))

    async def _handle_pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        if not self.cb:
            await update.message.reply_text("Circuit breaker tidak aktif.")
            return
        self.cb.manual_pause(reason="user_request")
        await update.message.reply_html("⏸ <b>Trading PAUSED</b>")

    async def _handle_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        if not self.cb:
            return
        self.cb.manual_resume(reason="user_request")
        await update.message.reply_html("▶️ <b>Trading RESUMED</b>")

    async def _handle_smartlist(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        if not self.registry:
            await update.message.reply_text("Registry tidak tersedia.")
            return

        top = self.registry.get_top_tier_wallets(max_count=10)
        if not top:
            await update.message.reply_text("Registry kosong. Run bootstrap-wallets dulu.")
            return

        lines = ["<b>🏆 TOP 10 SMART WALLETS</b>"]
        for sw in top:
            short_addr = f"{sw.address[:6]}...{sw.address[-4:]}"
            wr_str = f"{sw.winrate:.0%}" if sw.winrate else "?"
            lines.append(
                f"<b>{sw.tier}</b> <code>{short_addr}</code> "
                f"WR:{wr_str} P:{sw.realized_profit:.0f}"
            )
        await update.message.reply_html("\n".join(lines))

    async def _handle_addwallet(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        if not self.registry:
            await update.message.reply_text("Registry tidak tersedia.")
            return

        args = ctx.args or []
        if not args:
            await update.message.reply_text("Usage: /addwallet ADDRESS [A|B] [notes]")
            return

        address = args[0]
        tier = args[1].upper() if len(args) > 1 else "A"
        if tier not in ("A", "B"):
            await update.message.reply_text("Tier harus A atau B")
            return
        notes = " ".join(args[2:]) if len(args) > 2 else ""

        self.registry.add_manual(address=address, tier=tier, notes=notes)  # type: ignore
        await update.message.reply_html(
            f"✅ Added <code>{html.escape(address[:8])}...{html.escape(address[-6:])}</code> as MANUAL_{tier}"
        )

    async def _handle_blacklist(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        if not self.registry:
            return

        args = ctx.args or []
        if not args:
            await update.message.reply_text("Usage: /blacklist ADDRESS [notes]")
            return

        address = args[0]
        notes = " ".join(args[1:]) if len(args) > 1 else ""
        self.registry.add_blacklist(address=address, notes=notes)
        await update.message.reply_html(f"🚫 Blacklisted <code>{html.escape(address[:12])}...</code>")

    async def _handle_config(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        msg = (
            "<b>⚙️ CONFIG</b>\n"
            f"DRY_RUN: <code>{settings.dry_run}</code>\n"
            f"Max position: <code>{settings.max_position_size_sol} SOL</code>\n"
            f"Max concurrent: <code>{settings.max_concurrent_positions}</code>\n"
            f"Min score buy: <code>{settings.min_score_to_buy}</code>\n"
            f"Slippage: <code>{settings.slippage_bps / 100:.1f}%</code>\n"
            f"Hard SL: <code>{settings.hard_sl_pct}%</code>\n"
            f"TP1: +{settings.tp1_gain_pct}% sell {settings.tp1_sell_pct}%\n"
            f"TP2: +{settings.tp2_gain_pct}% sell {settings.tp2_sell_pct}%\n"
            f"TP3: +{settings.tp3_gain_pct}% sell {settings.tp3_sell_pct}%\n"
            f"CB enabled: <code>{settings.cb_enabled}</code>"
        )
        await update.message.reply_html(msg)

    async def _handle_stats(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return

        lines = ["<b>📊 STATS</b>"]

        if self.registry:
            summary = self.registry.stats_summary()
            lines.append("\n<i>Smart Wallets:</i>")
            for tier, count in sorted(summary.items()):
                lines.append(f"  {tier}: {count}")

        if self.cb:
            recent = self.cb.state.recent_trades
            if recent:
                wins = sum(1 for t in recent if t.won)
                wr = wins / len(recent) * 100
                lines.append(f"\n<i>Recent {len(recent)} trades:</i> {wr:.0f}% WR")

        await update.message.reply_html("\n".join(lines))

    async def _handle_apply_tuning(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Manual apply tuner recommendation. Safety: +/- 20% range guard."""
        if not self._is_authorized(update):
            return

        args = ctx.args or []
        if len(args) < 2:
            await update.message.reply_text("Usage: /applyTuning PARAM VALUE")
            return

        param, new_value_str = args[0], args[1]

        # Whitelist of tunable params
        ALLOWED = {"min_score_to_buy", "hard_sl_pct", "tp1_gain_pct", "max_position_size_sol"}
        if param not in ALLOWED:
            await update.message.reply_text(f"Not tunable: {param}. Allowed: {ALLOWED}")
            return

        try:
            new_value = float(new_value_str)
        except ValueError:
            await update.message.reply_text(f"Invalid value: {new_value_str}")
            return

        # Get current value
        current = getattr(settings, param, None)
        if current is None:
            await update.message.reply_text(f"Param not found: {param}")
            return

        # 20% range guard
        if abs(new_value - current) > abs(current * 0.20):
            await update.message.reply_text(
                f"Adjustment too large. Max +/- 20% from current {current}. "
                f"Allowed range: {current * 0.80:.4f} to {current * 1.20:.4f}"
            )
            return

        # Apply (in-memory only — to persist, user must edit .env and restart)
        setattr(settings, param, new_value)

        # Audit log
        import json as _json
        from datetime import datetime, timezone
        from pathlib import Path
        audit_path = Path("data/tuning_applied.json")
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit = []
        if audit_path.exists():
            try:
                audit = _json.loads(audit_path.read_text())
            except Exception:
                pass
        audit.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "param": param,
            "old_value": current,
            "new_value": new_value,
            "applied_by": "user_telegram",
        })
        audit_path.write_text(_json.dumps(audit[-50:], indent=2))  # FIFO 50

        log.info("apply_tuning_applied", param=param, old=current, new=new_value)
        await update.message.reply_html(
            f"✅ Applied <code>{param}</code>: {current} → {new_value}\n\n"
            f"<i>Note: in-memory only. Edit secrets/.env to persist across restart.</i>"
        )

    async def _handle_wallet_info(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Debug wallet assessment via LLM."""
        if not self._is_authorized(update):
            return
        args = ctx.args or []
        if not args:
            await update.message.reply_text("Usage: /walletinfo ADDRESS")
            return
        address = args[0]

        sw = self.registry.get_by_address(address) if self.registry else None
        if sw:
            msg = (
                f"<b>Wallet:</b> <code>{sw.address[:8]}...{sw.address[-6:]}</code>\n"
                f"<b>Tier:</b> {sw.tier}\n"
                f"<b>Winrate:</b> {sw.winrate:.1%}\n"
                f"<b>Profit:</b> {sw.realized_profit:.2f} SOL\n"
                f"<b>Trades:</b> {sw.buy_count + sw.sell_count}\n"
                f"<b>Source:</b> {sw.source}\n"
                f"<b>Notes:</b> {sw.notes[:100] if sw.notes else 'none'}"
            )
        else:
            msg = f"Wallet <code>{address[:12]}...</code> not in registry"

        await update.message.reply_html(msg)

    # ----------------------------------------------------------------------
    # Phase 10: Interactive menu + strategy hot-reload + alerts inspection
    # ----------------------------------------------------------------------

    async def _handle_menu(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Show the top-level inline-keyboard main menu."""
        if not self._is_authorized(update):
            return
        try:
            from src.infra.telegram_menus import build_main_menu
        except ImportError:
            await update.message.reply_text("Menus not available.")
            return
        markup = build_main_menu()
        await update.message.reply_html(
            "<b>🎛️ Bot Control Center</b>\nPilih action di bawah:",
            reply_markup=markup,
        )

    async def _handle_strategy(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Show strategy list / switch active strategy."""
        if not self._is_authorized(update):
            return
        if not self.strategy_manager:
            await update.message.reply_text("Strategy manager not configured.")
            return
        args = ctx.args or []
        if not args:
            try:
                strategies = await self.strategy_manager.list_all()
                lines = ["<b>📊 Available Strategies:</b>"]
                for s in strategies:
                    marker = "✓" if s.get("enabled") else "○"
                    lines.append(f"{marker} <code>{s['id']}</code> — {s.get('name', s['id'])}")
                lines.append("\nUsage: <code>/strategy &lt;id&gt;</code> to activate")
                await update.message.reply_html("\n".join(lines))
            except Exception as e:
                await update.message.reply_text(f"Failed to list: {e}")
            return
        target = args[0]
        try:
            ok = await self.strategy_manager.set_active(target)
            if ok:
                await update.message.reply_html(f"✓ Strategy <b>{target}</b> activated.")
            else:
                await update.message.reply_text(f"Strategy '{target}' not found.")
        except Exception as e:
            await update.message.reply_text(f"Failed: {e}")

    async def _handle_stratset(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Update a single param of a strategy: /stratset <strategy_id> <key> <value>."""
        if not self._is_authorized(update):
            return
        if not self.strategy_manager:
            await update.message.reply_text("Strategy manager not configured.")
            return
        args = ctx.args or []
        if len(args) < 3:
            await update.message.reply_text("Usage: /stratset <strategy_id> <key> <value>")
            return
        sid, key, value = args[0], args[1], args[2]
        try:
            ok = await self.strategy_manager.update_config(sid, key, value)
            if ok:
                await update.message.reply_html(
                    f"✓ <b>{sid}.{key}</b> = <code>{value}</code>"
                )
            else:
                await update.message.reply_text("Update returned 0 rows. Check strategy id.")
        except Exception as e:
            await update.message.reply_text(f"Failed: {e}")

    async def _handle_alerts(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """List pending price alerts (dip-buy / wait-for-dump)."""
        if not self._is_authorized(update):
            return
        if not self.price_alert_manager:
            await update.message.reply_text("Price alerts not configured.")
            return
        try:
            alerts = await self.price_alert_manager.list_pending()
            if not alerts:
                await update.message.reply_text("No pending price alerts.")
                return
            lines = ["<b>⏳ Pending Price Alerts:</b>"]
            for a in alerts[:10]:
                target = a.get("target_price_usd") or a.get("target_ath_distance_pct", "?")
                lines.append(
                    f"<code>{a['mint'][:8]}...</code> {a.get('symbol', '')} "
                    f"@ {target} (type: {a.get('alert_type', '?')})"
                )
            await update.message.reply_html("\n".join(lines))
        except Exception as e:
            await update.message.reply_text(f"Failed: {e}")

    async def _handle_feeclaims(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Show recent Pump.fun fee-claim events."""
        if not self._is_authorized(update):
            return
        bot_ref = getattr(self.callback_router, "bot_ref", None) if self.callback_router else None
        agg = getattr(bot_ref, "feeclaim_aggregator", None) if bot_ref else None
        if not agg:
            await update.message.reply_text("Fee-claim listener not configured.")
            return
        events = agg.get_recent_events(limit=10)
        if not events:
            await update.message.reply_text("No recent fee-claim events.")
            return
        lines = ["<b>💸 Recent Fee Claims:</b>"]
        for e in events:
            lines.append(
                f"<code>{e.mint[:8]}...</code> {e.distributed_sol:.3f} SOL "
                f"({len(e.shareholders)} holders)"
            )
        await update.message.reply_html("\n".join(lines))
