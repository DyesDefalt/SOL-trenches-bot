"""
Phase 10: Callback router for Telegram inline-keyboard interactions.

Parses callback_query.data of the form "menu:ACTION:arg1:arg2…",
dispatches to the appropriate handler, always answers the query so
Telegram removes the loading spinner, and edits the existing message
in-place (no new message spam).

Authorization: every handler silently ignores requests whose
from_user.id does not match settings.telegram_chat_id.

Usage (wire into TelegramBot._build_app):
    from src.infra.telegram_callbacks import CallbackRouter
    from telegram.ext import CallbackQueryHandler

    router = CallbackRouter(bot_ref=self)
    app.add_handler(CallbackQueryHandler(router.handle_callback))
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING, Any

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from src.config import settings
from src.infra.logger import get_logger
from src.infra.telegram_menus import (
    build_alerts_menu,
    build_back_menu,
    build_confirm_intent_menu,
    build_main_menu,
    build_position_detail_menu,
    build_positions_menu,
    build_settings_menu,
    build_strategy_detail_menu,
    build_strategy_menu,
)

if TYPE_CHECKING:
    pass  # bot_ref typing deferred to avoid circular imports

log = get_logger(__name__)


class CallbackRouter:
    """Routes inline-keyboard callbacks to domain handlers."""

    def __init__(self, bot_ref: Any) -> None:
        """
        bot_ref: the main Bot instance.  Expected attributes (all optional,
        checked before use):
            .position_manager   — PositionManager
            .strategy_manager   — StrategyManager
            .db                 — Database
            .price_alerts       — PriceAlertManager
            .cb                 — CircuitBreaker
        """
        self._bot = bot_ref

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def handle_callback(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Main dispatcher.  Called by CallbackQueryHandler."""
        query = update.callback_query
        if query is None:
            return

        # Authorization — silent drop for non-owners
        if not self._is_authorized(update):
            await query.answer()
            return

        data: str = query.data or ""
        parts = data.split(":")

        # Expected prefix: "menu"
        if not parts or parts[0] != "menu":
            await query.answer()
            return

        action = parts[1] if len(parts) > 1 else ""
        args = parts[2:] if len(parts) > 2 else []

        try:
            await self._dispatch(query, action, args)
        except Exception as exc:
            log.warning("callback_handler_error", action=action, error=str(exc))
            await query.answer(text="⚠️ Error — try again", show_alert=False)

    # ------------------------------------------------------------------
    # Dispatcher
    # ------------------------------------------------------------------

    async def _dispatch(self, query: Any, action: str, args: list[str]) -> None:
        """Route action to the correct handler."""
        # Navigation / menu display
        if action == "m":
            await self._handle_menu_navigation(query, args)
        elif action == "bk":
            target = args[0] if args else "main"
            await self._handle_menu_navigation(query, [target])

        # Strategy list tap → show detail
        elif action == "s":
            strategy_id = args[0] if args else ""
            await self._handle_strategy_detail(query, strategy_id)

        # Strategy activate
        elif action == "sa":
            strategy_id = args[0] if args else ""
            await self._handle_strategy_switch(query, strategy_id)

        # Positions
        elif action == "p":
            position_id = args[0] if args else ""
            await self._handle_position_detail(query, position_id)

        # Position sell
        elif action == "ps":
            pct_or_type = args[0] if args else "100"
            pos_id = args[1] if len(args) > 1 else ""
            await self._handle_position_sell(query, pct_or_type, pos_id)

        # Alert cancel
        elif action == "ac":
            alert_id = args[0] if args else ""
            await self._handle_alert_cancel(query, alert_id)

        # Settings view / edit (read-only info for now)
        elif action == "se":
            await self._handle_settings(query, args)

        # Confirm intent (approve / reject)
        elif action == "ci":
            intent_action = args[0] if args else ""
            intent_id = args[1] if len(args) > 1 else ""
            await self._handle_confirm_intent(query, intent_id, intent_action)

        else:
            await query.answer(text="Unknown action")

    # ------------------------------------------------------------------
    # Navigation handlers
    # ------------------------------------------------------------------

    async def _handle_menu_navigation(self, query: Any, args: list[str]) -> None:
        """Handle back / main navigation and top-level section renders."""
        target = args[0] if args else "main"
        page = int(args[1]) if len(args) > 1 and args[1].isdigit() else 0

        await query.answer()

        if target == "main":
            await query.edit_message_text(
                text="<b>Main Menu</b> — choose a section:",
                parse_mode=ParseMode.HTML,
                reply_markup=build_main_menu(),
            )

        elif target == "strategy":
            strategies, active_id = await self._fetch_strategies()
            await query.edit_message_text(
                text="<b>Strategy</b> — tap to view / activate:",
                parse_mode=ParseMode.HTML,
                reply_markup=build_strategy_menu(strategies, active_id),
            )

        elif target == "positions":
            positions = self._fetch_positions()
            await query.edit_message_text(
                text="<b>Open Positions</b>:",
                parse_mode=ParseMode.HTML,
                reply_markup=build_positions_menu(positions, page),
            )

        elif target == "alerts":
            alerts = await self._fetch_alerts()
            await query.edit_message_text(
                text="<b>Price Alerts</b> — pending:",
                parse_mode=ParseMode.HTML,
                reply_markup=build_alerts_menu(alerts, page),
            )

        elif target == "settings":
            cfg = await self._current_settings()
            await query.edit_message_text(
                text="<b>Settings</b>:",
                parse_mode=ParseMode.HTML,
                reply_markup=build_settings_menu(cfg),
            )

        elif target == "help":
            text = (
                "<b>Help</b>\n\n"
                "Use the menus to:\n"
                "• <b>Strategy</b> — switch or review active strategy\n"
                "• <b>Positions</b> — view open trades, trigger manual sells\n"
                "• <b>Alerts</b> — manage dip-buy price alerts\n"
                "• <b>Settings</b> — view current config values\n"
                "• <b>Pause/Resume</b> — halt or restart trading\n\n"
                "Or use slash commands: /status /pnl /config"
            )
            await query.edit_message_text(
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=build_back_menu("main"),
            )

        elif target == "stats":
            text = await self._build_stats_text()
            await query.edit_message_text(
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=build_back_menu("main"),
            )

        elif target == "lessons":
            await query.edit_message_text(
                text="<b>Lessons</b>\n\n(Use /lessons command for full list.)",
                parse_mode=ParseMode.HTML,
                reply_markup=build_back_menu("main"),
            )

        elif target == "pause":
            await self._handle_pause(query)

        else:
            await query.edit_message_text(
                text="<b>Main Menu</b>:",
                parse_mode=ParseMode.HTML,
                reply_markup=build_main_menu(),
            )

    # ------------------------------------------------------------------
    # Strategy handlers
    # ------------------------------------------------------------------

    async def _handle_strategy_detail(self, query: Any, strategy_id: str) -> None:
        """Show parameter detail for a specific strategy."""
        await query.answer()
        sm = getattr(self._bot, "strategy_manager", None)
        if sm is None or not strategy_id:
            await query.edit_message_text(
                text="Strategy manager unavailable.",
                reply_markup=build_back_menu("strategy"),
            )
            return

        strat = await sm.get_by_id(strategy_id)
        if strat is None:
            await query.edit_message_text(
                text=f"Strategy <code>{html.escape(strategy_id)}</code> not found.",
                parse_mode=ParseMode.HTML,
                reply_markup=build_back_menu("strategy"),
            )
            return

        text = (
            f"<b>{html.escape(strat.get('name', strategy_id))}</b>\n"
            f"Status: {'✓ active' if strat.get('enabled') else '○ inactive'}"
        )
        await query.edit_message_text(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=build_strategy_detail_menu(strat),
        )

    async def _handle_strategy_switch(self, query: Any, strategy_id: str) -> None:
        """Activate a strategy by id."""
        await query.answer()
        sm = getattr(self._bot, "strategy_manager", None)
        if sm is None:
            await query.edit_message_text(
                text="Strategy manager unavailable.",
                reply_markup=build_back_menu("strategy"),
            )
            return

        ok = await sm.set_active(strategy_id)
        if ok:
            strategies, active_id = await self._fetch_strategies()
            await query.edit_message_text(
                text=f"✅ Strategy <b>{html.escape(strategy_id)}</b> activated.",
                parse_mode=ParseMode.HTML,
                reply_markup=build_strategy_menu(strategies, active_id),
            )
        else:
            await query.edit_message_text(
                text=f"❌ Could not activate <code>{html.escape(strategy_id)}</code>.",
                parse_mode=ParseMode.HTML,
                reply_markup=build_back_menu("strategy"),
            )

    # ------------------------------------------------------------------
    # Position handlers
    # ------------------------------------------------------------------

    async def _handle_position_detail(self, query: Any, position_id: str) -> None:
        """Show sell controls for a single position."""
        await query.answer()
        positions = self._fetch_positions()
        try:
            pid = int(position_id)
        except (ValueError, TypeError):
            pid = -1

        pos = next((p for p in positions if p.get("db_id") == pid), None)
        if pos is None:
            await query.edit_message_text(
                text=f"Position #{html.escape(str(position_id))} not found.",
                parse_mode=ParseMode.HTML,
                reply_markup=build_back_menu("positions"),
            )
            return

        sym = html.escape(pos.get("token_symbol", "???"))
        gain = pos.get("gain_pct", 0.0)
        sign = "+" if gain >= 0 else ""
        text = (
            f"<b>{sym}</b>\n"
            f"PnL: {sign}{gain:.2f}%\n"
            f"Entry: ${pos.get('entry_price_usd', 0):.6f}\n"
            f"Remaining: {pos.get('amount_remaining_token', 0):,.0f} tokens"
        )
        await query.edit_message_text(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=build_position_detail_menu(pos),
        )

    async def _handle_position_sell(
        self, query: Any, pct_or_type: str, position_id: str
    ) -> None:
        """Force-sell a position by percentage or trigger type (tp/sl)."""
        await query.answer(text=f"Sell {pct_or_type} queued…")
        pm = getattr(self._bot, "position_manager", None)
        if pm is None:
            await query.edit_message_text(
                text="Position manager unavailable.",
                reply_markup=build_back_menu("positions"),
            )
            return

        try:
            pid = int(position_id)
        except (ValueError, TypeError):
            await query.edit_message_text(
                text="Invalid position ID.",
                reply_markup=build_back_menu("positions"),
            )
            return

        # Delegate — position_manager.force_sell expected to exist
        force_sell = getattr(pm, "force_sell", None)
        if force_sell is not None:
            await force_sell(pid, pct_or_type)

        # Refresh positions view
        positions = self._fetch_positions()
        await query.edit_message_text(
            text=f"✅ Sell <b>{html.escape(pct_or_type)}</b> triggered for position #{pid}.",
            parse_mode=ParseMode.HTML,
            reply_markup=build_positions_menu(positions),
        )

    # ------------------------------------------------------------------
    # Alert handlers
    # ------------------------------------------------------------------

    async def _handle_alert_cancel(self, query: Any, alert_id: str) -> None:
        """Cancel a pending price alert."""
        await query.answer(text="Cancelling alert…")
        pa = getattr(self._bot, "price_alerts", None)
        if pa is not None:
            cancel = getattr(pa, "cancel_alert", None)
            if cancel is not None:
                try:
                    await cancel(int(alert_id))
                except Exception as exc:
                    log.warning("alert_cancel_failed", alert_id=alert_id, error=str(exc))

        alerts = await self._fetch_alerts()
        await query.edit_message_text(
            text=f"❌ Alert #{html.escape(str(alert_id))} cancelled.",
            parse_mode=ParseMode.HTML,
            reply_markup=build_alerts_menu(alerts),
        )

    # ------------------------------------------------------------------
    # Settings handler
    # ------------------------------------------------------------------

    async def _handle_settings(self, query: Any, args: list[str]) -> None:
        """Show settings menu.  Edit flow not yet implemented (info-only)."""
        await query.answer()
        cfg = await self._current_settings()
        sub = args[0] if args else "view"
        if sub == "edit":
            key = args[1] if len(args) > 1 else ""
            await query.edit_message_text(
                text=f"<b>Edit {html.escape(key)}</b>\n\nUse /applyTuning {html.escape(key)} VALUE to update.",
                parse_mode=ParseMode.HTML,
                reply_markup=build_back_menu("settings"),
            )
        else:
            await query.edit_message_text(
                text="<b>Settings</b>:",
                parse_mode=ParseMode.HTML,
                reply_markup=build_settings_menu(cfg),
            )

    # ------------------------------------------------------------------
    # Confirm-intent handler
    # ------------------------------------------------------------------

    async def _handle_confirm_intent(
        self, query: Any, intent_id: str, action: str
    ) -> None:
        """Approve or reject a confirm-mode trade intent."""
        approved = action == "ok"
        await query.answer(text="Approved ✅" if approved else "Rejected ❌")

        # Delegate to bot if it exposes a confirm_intent method
        ci_handler = getattr(self._bot, "confirm_intent", None)
        if ci_handler is not None:
            await ci_handler(intent_id, approved=approved)

        label = "✅ Approved" if approved else "❌ Rejected"
        await query.edit_message_text(
            text=f"{label} — intent <code>{html.escape(intent_id)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=build_back_menu("main"),
        )

    # ------------------------------------------------------------------
    # Pause / Resume shortcut
    # ------------------------------------------------------------------

    async def _handle_pause(self, query: Any) -> None:
        """Toggle pause/resume via circuit breaker."""
        cb = getattr(self._bot, "cb", None)
        if cb is None:
            await query.edit_message_text(
                text="Circuit breaker not available.",
                reply_markup=build_back_menu("main"),
            )
            return

        is_paused = getattr(cb.state, "is_paused", False)
        if is_paused:
            cb.manual_resume(reason="menu_user_request")
            label = "▶️ Trading RESUMED"
        else:
            cb.manual_pause(reason="menu_user_request")
            label = "⏸ Trading PAUSED"

        await query.edit_message_text(
            text=f"<b>{label}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=build_main_menu(),
        )

    # ------------------------------------------------------------------
    # Data-fetch helpers (stateless each call)
    # ------------------------------------------------------------------

    async def _fetch_strategies(self) -> tuple[list[dict], str]:
        """Return (all_strategies, active_id)."""
        sm = getattr(self._bot, "strategy_manager", None)
        if sm is None:
            return [], ""
        all_strats: list[dict] = await sm.list_all()
        active = next((s["id"] for s in all_strats if s.get("enabled")), "")
        return all_strats, active

    def _fetch_positions(self) -> list[dict]:
        """Return list of position dicts from in-memory position manager."""
        pm = getattr(self._bot, "position_manager", None)
        if pm is None:
            return []
        positions_map: dict = getattr(pm, "_positions", {})
        result = []
        for addr, pos in positions_map.items():
            entry = getattr(pos, "entry_price_usd", 0.0)
            peak = getattr(pos, "peak_price_usd", entry)
            gain_pct = ((peak - entry) / entry * 100) if entry else 0.0
            result.append(
                {
                    "db_id": getattr(pos, "db_id", 0),
                    "token_address": addr,
                    "token_symbol": getattr(pos, "token_symbol", "???"),
                    "entry_price_usd": entry,
                    "peak_price_usd": peak,
                    "gain_pct": gain_pct,
                    "amount_remaining_token": getattr(pos, "amount_remaining_token", 0),
                }
            )
        return result

    async def _fetch_alerts(self) -> list[dict]:
        """Return pending price alerts from DB."""
        db = getattr(self._bot, "db", None)
        if db is None:
            return []
        # Attempt a generic query; gracefully degrade if method absent
        get_alerts = getattr(db, "get_pending_alerts", None)
        if get_alerts is None:
            return []
        try:
            return await get_alerts()
        except Exception as exc:
            log.warning("fetch_alerts_failed", error=str(exc))
            return []

    async def _current_settings(self) -> dict:
        """Return current active strategy config or env settings dict."""
        sm = getattr(self._bot, "strategy_manager", None)
        if sm is not None:
            try:
                return await sm.get_active()
            except Exception:
                pass
        # Fallback: env settings as plain dict
        return {
            "min_score_to_buy": settings.min_score_to_buy,
            "max_position_size_sol": settings.max_position_size_sol,
            "hard_sl_pct": settings.hard_sl_pct,
            "tp1_gain_pct": settings.tp1_gain_pct,
            "slippage_bps": settings.slippage_bps,
            "max_concurrent_positions": settings.max_concurrent_positions,
        }

    async def _build_stats_text(self) -> str:
        lines = ["<b>📊 Stats</b>"]
        registry = getattr(self._bot, "registry", None)
        if registry:
            summary = registry.stats_summary()
            for tier, count in sorted(summary.items()):
                lines.append(f"  {tier}: {count}")
        cb = getattr(self._bot, "cb", None)
        if cb:
            recent = getattr(cb.state, "recent_trades", [])
            if recent:
                wins = sum(1 for t in recent if getattr(t, "won", False))
                wr = wins / len(recent) * 100
                lines.append(f"Recent {len(recent)} trades: {wr:.0f}% WR")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Authorization
    # ------------------------------------------------------------------

    def _is_authorized(self, update: Update) -> bool:
        """Only allow configured chat owner."""
        query = update.callback_query
        if query is None:
            return False
        user = query.from_user
        if user is None:
            return False
        return str(user.id) == str(settings.telegram_chat_id)
