"""
Phase 10: Tests for telegram_menus.py and telegram_callbacks.py.

No live Telegram connection required — python-telegram-bot objects are
real (InlineKeyboardMarkup etc.) but Update / CallbackQuery are mocked
with simple dataclasses / MagicMocks.

Run:
    . venv/bin/activate && pytest tests/test_telegram_menus.py -v
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram import InlineKeyboardMarkup

# Module under test
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
from src.infra.telegram_callbacks import CallbackRouter


# ---------------------------------------------------------------------------
# Fake Telegram objects
# ---------------------------------------------------------------------------

@dataclass
class FakeUser:
    id: int
    is_bot: bool = False
    first_name: str = "Tester"


@dataclass
class FakeCallbackQuery:
    data: str
    from_user: FakeUser
    _answer_calls: list[dict] = field(default_factory=list)
    _edit_calls: list[dict] = field(default_factory=list)

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self._answer_calls.append({"text": text, "show_alert": show_alert})

    async def edit_message_text(
        self, text: str, parse_mode: str = "", reply_markup: Any = None
    ) -> None:
        self._edit_calls.append(
            {"text": text, "parse_mode": parse_mode, "reply_markup": reply_markup}
        )


@dataclass
class FakeUpdate:
    callback_query: FakeCallbackQuery | None = None


def _make_update(data: str, user_id: int = 123456) -> FakeUpdate:
    user = FakeUser(id=user_id)
    cq = FakeCallbackQuery(data=data, from_user=user)
    return FakeUpdate(callback_query=cq)


def _make_bot_ref(
    strategies: list[dict] | None = None,
    active_id: str = "balanced",
    positions: dict | None = None,
) -> MagicMock:
    bot = MagicMock()

    # Strategy manager
    sm = MagicMock()
    strats = strategies or [
        {"id": "balanced", "name": "Balanced", "enabled": True},
        {"id": "aggressive", "name": "Aggressive", "enabled": False},
    ]
    sm.list_all = AsyncMock(return_value=strats)
    sm.get_by_id = AsyncMock(
        side_effect=lambda sid: next((s for s in strats if s["id"] == sid), None)
    )
    sm.set_active = AsyncMock(return_value=True)
    sm.get_active = AsyncMock(return_value={"min_score_to_buy": 70, "hard_sl_pct": -45})
    bot.strategy_manager = sm

    # Position manager
    pm = MagicMock()
    pos_dict = positions or {}
    pm._positions = pos_dict
    bot.position_manager = pm

    # DB (no alerts by default)
    db = MagicMock()
    db.get_pending_alerts = AsyncMock(return_value=[])
    bot.db = db

    return bot


# ---------------------------------------------------------------------------
# 1. Menu builder return types
# ---------------------------------------------------------------------------

def test_build_main_menu_returns_markup():
    markup = build_main_menu()
    assert isinstance(markup, InlineKeyboardMarkup)
    # 4 rows of 2 buttons each
    assert len(markup.inline_keyboard) == 4
    for row in markup.inline_keyboard:
        assert len(row) == 2


def test_build_strategy_menu_returns_markup():
    strats = [
        {"id": "balanced", "name": "Balanced"},
        {"id": "aggressive", "name": "Aggressive"},
    ]
    markup = build_strategy_menu(strats, active_id="balanced")
    assert isinstance(markup, InlineKeyboardMarkup)
    # 2 strat rows + 1 nav row
    assert len(markup.inline_keyboard) == 3


def test_build_positions_menu_returns_markup():
    markup = build_positions_menu([])
    assert isinstance(markup, InlineKeyboardMarkup)


def test_build_position_detail_menu_returns_markup():
    pos = {"db_id": 42, "token_symbol": "BONK"}
    markup = build_position_detail_menu(pos)
    assert isinstance(markup, InlineKeyboardMarkup)


def test_build_alerts_menu_returns_markup():
    markup = build_alerts_menu([])
    assert isinstance(markup, InlineKeyboardMarkup)


def test_build_settings_menu_returns_markup():
    cfg = {"min_score_to_buy": 70, "hard_sl_pct": -45, "tp1_gain_pct": 80}
    markup = build_settings_menu(cfg)
    assert isinstance(markup, InlineKeyboardMarkup)


def test_build_confirm_intent_menu_returns_markup():
    markup = build_confirm_intent_menu("intent-abc")
    assert isinstance(markup, InlineKeyboardMarkup)
    # One row, two buttons
    assert len(markup.inline_keyboard) == 1
    assert len(markup.inline_keyboard[0]) == 2


def test_build_back_menu_returns_markup():
    markup = build_back_menu("main")
    assert isinstance(markup, InlineKeyboardMarkup)
    assert len(markup.inline_keyboard) == 1
    assert len(markup.inline_keyboard[0]) == 1


# ---------------------------------------------------------------------------
# 2. Callback data format — parseable and under 64 bytes
# ---------------------------------------------------------------------------

def _all_buttons(markup: InlineKeyboardMarkup) -> list[Any]:
    return [btn for row in markup.inline_keyboard for btn in row]


def _check_callback_data(markup: InlineKeyboardMarkup) -> None:
    for btn in _all_buttons(markup):
        data: str = btn.callback_data or ""
        assert data.startswith("menu:"), f"Bad prefix: {data!r}"
        parts = data.split(":")
        assert len(parts) >= 2, f"Too few parts: {data!r}"
        assert len(data.encode()) <= 64, f"callback_data too long: {data!r}"


def test_main_menu_callback_data_format():
    _check_callback_data(build_main_menu())


def test_strategy_menu_callback_data_format():
    strats = [{"id": "balanced", "name": "Balanced"}, {"id": "aggressive", "name": "Aggressive"}]
    _check_callback_data(build_strategy_menu(strats, "balanced"))


def test_position_detail_callback_data_format():
    _check_callback_data(build_position_detail_menu({"db_id": 999, "token_symbol": "BONK"}))


def test_confirm_intent_callback_data_format():
    _check_callback_data(build_confirm_intent_menu("trade-intent-0001"))


def test_callback_data_under_64_bytes_with_long_strategy_id():
    """Even a longish strategy slug must stay under 64 bytes."""
    strats = [{"id": "my_very_long_strategy_name_slug", "name": "Long"}]
    _check_callback_data(build_strategy_menu(strats, "my_very_long_strategy_name_slug"))


# ---------------------------------------------------------------------------
# 3. Strategy menu marks active correctly
# ---------------------------------------------------------------------------

def test_strategy_menu_active_marker():
    strats = [
        {"id": "balanced", "name": "Balanced"},
        {"id": "aggressive", "name": "Aggressive"},
        {"id": "conservative", "name": "Conservative"},
    ]
    markup = build_strategy_menu(strats, active_id="aggressive")
    buttons = _all_buttons(markup)
    labels = [btn.text for btn in buttons]

    # Find the active one
    active_labels = [lbl for lbl in labels if lbl.startswith("✓")]
    inactive_labels = [lbl for lbl in labels if lbl.startswith("○")]

    assert len(active_labels) == 1
    assert "Aggressive" in active_labels[0]
    assert len(inactive_labels) == 2


def test_strategy_menu_no_active():
    strats = [{"id": "balanced", "name": "Balanced"}]
    markup = build_strategy_menu(strats, active_id="")
    buttons = _all_buttons(markup)
    active_labels = [btn.text for btn in buttons if btn.text.startswith("✓")]
    assert len(active_labels) == 0


# ---------------------------------------------------------------------------
# 4. Positions menu handles empty list gracefully
# ---------------------------------------------------------------------------

def test_positions_menu_empty():
    markup = build_positions_menu([])
    buttons = _all_buttons(markup)
    texts = [btn.text for btn in buttons]
    assert any("no open positions" in t for t in texts)


def test_positions_menu_single_item():
    positions = [{"db_id": 1, "token_symbol": "BONK", "gain_pct": 12.5}]
    markup = build_positions_menu(positions)
    buttons = _all_buttons(markup)
    texts = [btn.text for btn in buttons]
    assert any("BONK" in t for t in texts)
    assert any("+12.5%" in t for t in texts)


def test_positions_menu_pagination():
    # 10 positions → page 0 shows 8, page 1 shows 2
    positions = [{"db_id": i, "token_symbol": f"TK{i}", "gain_pct": float(i)} for i in range(10)]
    markup_p0 = build_positions_menu(positions, page=0)
    markup_p1 = build_positions_menu(positions, page=1)

    # Page 0: 8 position rows + 1 nav row (Back + Next)
    assert len(markup_p0.inline_keyboard) == 9
    # Page 1: 2 position rows + 1 nav row (Prev + Back)
    assert len(markup_p1.inline_keyboard) == 3


# ---------------------------------------------------------------------------
# 5. Position detail menu has expected buttons
# ---------------------------------------------------------------------------

def test_position_detail_menu_buttons():
    pos = {"db_id": 7, "token_symbol": "WIF"}
    markup = build_position_detail_menu(pos)
    buttons = _all_buttons(markup)
    texts = [btn.text for btn in buttons]

    assert any("25%" in t for t in texts)
    assert any("50%" in t for t in texts)
    assert any("100%" in t for t in texts)
    assert any("Force TP" in t for t in texts)
    assert any("Force SL" in t for t in texts)
    assert any("Back" in t for t in texts)


# ---------------------------------------------------------------------------
# 6. CallbackRouter parses data correctly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_callback_router_parses_strategy():
    """Router dispatches 'menu:s:balanced' to strategy detail handler."""
    bot = _make_bot_ref()
    router = CallbackRouter(bot_ref=bot)

    # Patch the strategy detail handler to track calls
    called_with: list[str] = []

    async def fake_detail(query, strategy_id):
        called_with.append(strategy_id)
        await query.answer()

    router._handle_strategy_detail = fake_detail

    update = _make_update("menu:s:balanced", user_id=123456)

    with patch.object(router, "_is_authorized", return_value=True):
        await router.handle_callback(update, MagicMock())

    assert called_with == ["balanced"]


@pytest.mark.asyncio
async def test_callback_router_parses_position_sell():
    """Router dispatches 'menu:ps:50:42' to position sell handler."""
    bot = _make_bot_ref()
    router = CallbackRouter(bot_ref=bot)

    sell_calls: list[tuple] = []

    async def fake_sell(query, pct, pos_id):
        sell_calls.append((pct, pos_id))
        await query.answer()

    router._handle_position_sell = fake_sell

    update = _make_update("menu:ps:50:42", user_id=123456)
    with patch.object(router, "_is_authorized", return_value=True):
        await router.handle_callback(update, MagicMock())

    assert sell_calls == [("50", "42")]


# ---------------------------------------------------------------------------
# 7. CallbackRouter rejects unauthorized users
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_callback_router_rejects_unauthorized():
    """Unauthorized user_id triggers silent drop (only query.answer called)."""
    bot = _make_bot_ref()
    router = CallbackRouter(bot_ref=bot)

    update = _make_update("menu:m:main", user_id=999)  # wrong user

    dispatch_called = []
    original_dispatch = router._dispatch

    async def tracked_dispatch(query, action, args):
        dispatch_called.append(True)
        await original_dispatch(query, action, args)

    router._dispatch = tracked_dispatch

    with patch("src.infra.telegram_callbacks.settings") as mock_settings:
        mock_settings.telegram_chat_id = "123456"  # different from 999
        await router.handle_callback(update, MagicMock())

    # _dispatch should NOT have been called
    assert dispatch_called == []
    # But answer() was called to clear the spinner
    assert len(update.callback_query._answer_calls) == 1


@pytest.mark.asyncio
async def test_callback_router_unknown_prefix_ignored():
    """Callback data without 'menu:' prefix is answered but not dispatched."""
    bot = _make_bot_ref()
    router = CallbackRouter(bot_ref=bot)
    update = _make_update("other:stuff:here", user_id=123456)

    dispatch_called = []
    original_dispatch = router._dispatch

    async def tracked_dispatch(*args):
        dispatch_called.append(True)

    router._dispatch = tracked_dispatch

    with patch.object(router, "_is_authorized", return_value=True):
        await router.handle_callback(update, MagicMock())

    assert dispatch_called == []


# ---------------------------------------------------------------------------
# 8. Strategy detail menu — params from config
# ---------------------------------------------------------------------------

def test_strategy_detail_menu_shows_params():
    strategy = {
        "id": "balanced",
        "name": "Balanced",
        "enabled": True,
        "config": {
            "tp1_gain_pct": 80,
            "tp2_gain_pct": 150,
            "hard_sl_pct": -45,
            "trailing_stop_pct": 30,
            "min_score_to_buy": 70,
        },
    }
    markup = build_strategy_detail_menu(strategy)
    assert isinstance(markup, InlineKeyboardMarkup)
    texts = [btn.text for row in markup.inline_keyboard for btn in row]

    assert any("80" in t for t in texts)    # TP1
    assert any("150" in t for t in texts)   # TP2
    assert any("Activate" in t for t in texts)
    assert any("Back" in t for t in texts)


def test_strategy_detail_menu_activate_callback():
    strategy = {
        "id": "aggressive",
        "name": "Aggressive",
        "enabled": False,
        "config": {"tp1_gain_pct": 60},
    }
    markup = build_strategy_detail_menu(strategy)
    buttons = _all_buttons(markup)
    activate_btn = next((b for b in buttons if "Activate" in b.text), None)
    assert activate_btn is not None
    assert activate_btn.callback_data == "menu:sa:aggressive"


# ---------------------------------------------------------------------------
# 9. Confirm-intent menu approve / reject callbacks
# ---------------------------------------------------------------------------

def test_confirm_intent_approve_callback():
    markup = build_confirm_intent_menu("abc123")
    buttons = _all_buttons(markup)
    approve = next(b for b in buttons if "Approve" in b.text)
    reject = next(b for b in buttons if "Reject" in b.text)
    assert approve.callback_data == "menu:ci:ok:abc123"
    assert reject.callback_data == "menu:ci:no:abc123"


# ---------------------------------------------------------------------------
# 10. Alerts menu cancel callback
# ---------------------------------------------------------------------------

def test_alerts_menu_cancel_callback():
    alerts = [
        {
            "id": 77,
            "symbol": "BONK",
            "alert_type": "dip_target",
            "target_price_usd": 0.00001234,
            "target_ath_distance_pct": None,
        }
    ]
    markup = build_alerts_menu(alerts)
    buttons = _all_buttons(markup)
    cancel_btn = next((b for b in buttons if b.callback_data == "menu:ac:77"), None)
    assert cancel_btn is not None


# ---------------------------------------------------------------------------
# 11. Back menu callback data
# ---------------------------------------------------------------------------

def test_back_menu_callback_targets_correct_section():
    for target in ("main", "strategy", "positions", "alerts"):
        markup = build_back_menu(target)
        btn = markup.inline_keyboard[0][0]
        assert btn.callback_data == f"menu:bk:{target}"


# ---------------------------------------------------------------------------
# 12. CallbackRouter navigation dispatches to menu targets
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_callback_router_main_navigation():
    """menu:m:main should call edit_message_text with main menu markup."""
    bot = _make_bot_ref()
    router = CallbackRouter(bot_ref=bot)
    update = _make_update("menu:m:main")

    with patch.object(router, "_is_authorized", return_value=True):
        await router.handle_callback(update, MagicMock())

    cq = update.callback_query
    assert len(cq._edit_calls) == 1
    edit = cq._edit_calls[0]
    assert isinstance(edit["reply_markup"], InlineKeyboardMarkup)


@pytest.mark.asyncio
async def test_callback_router_back_to_main():
    """menu:bk:main should also render the main menu."""
    bot = _make_bot_ref()
    router = CallbackRouter(bot_ref=bot)
    update = _make_update("menu:bk:main")

    with patch.object(router, "_is_authorized", return_value=True):
        await router.handle_callback(update, MagicMock())

    cq = update.callback_query
    assert len(cq._edit_calls) == 1
