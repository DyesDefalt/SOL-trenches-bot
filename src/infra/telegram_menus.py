"""
Phase 10: Inline-keyboard menu builders for the Telegram bot.

Each function returns an InlineKeyboardMarkup ready to attach to a message.
Callback-data format:  "menu:ACTION:arg1:arg2"  (colon-delimited, ≤ 64 bytes)

Short-action aliases kept to guarantee the 64-byte Telegram hard limit:
    m   = main
    s   = strategy / strategy-detail
    sa  = strategy-activate
    p   = positions / position-detail
    ps  = position-sell
    al  = alerts
    ac  = alert-cancel
    se  = settings
    ci  = confirm-intent (approve / reject)
    bk  = back navigation

Usage:
    from src.infra.telegram_menus import build_main_menu
    markup = build_main_menu()
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_PAGE_SIZE = 8  # max items per paginated list


def _btn(label: str, data: str) -> InlineKeyboardButton:
    """Convenience wrapper — enforces the 64-byte callback_data limit."""
    if len(data.encode()) > 64:
        raise ValueError(f"callback_data too long ({len(data.encode())} bytes): {data!r}")
    return InlineKeyboardButton(text=label, callback_data=data)


def _back_btn(target: str = "main") -> InlineKeyboardButton:
    return _btn("« Back", f"menu:bk:{target}")


# ---------------------------------------------------------------------------
# Public menu builders
# ---------------------------------------------------------------------------


def build_main_menu() -> InlineKeyboardMarkup:
    """Top-level menu.  8 tiles, 2-per-row."""
    keyboard = [
        [_btn("► Strategy", "menu:m:strategy"), _btn("• Positions", "menu:m:positions")],
        [_btn("⚙ Settings",  "menu:m:settings"),  _btn("📊 Stats",     "menu:m:stats")],
        [_btn("⚠️ Alerts",   "menu:m:alerts"),    _btn("• Lessons",   "menu:m:lessons")],
        [_btn("⏸ Pause",     "menu:m:pause"),      _btn("• Help",      "menu:m:help")],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_strategy_menu(
    strategies: list[dict],
    active_id: str,
) -> InlineKeyboardMarkup:
    """
    One row per strategy.  Active one is prefixed with ✓, others with ○.
    Last row: [Back] [Settings for active].

    strategies: list of dicts with at minimum {"id": str, "name": str}.
    active_id:  id of the currently-enabled strategy (may be empty string).
    """
    keyboard: list[list[InlineKeyboardButton]] = []

    for strat in strategies:
        sid = strat["id"]
        name = strat.get("name", sid)
        prefix = "✓" if sid == active_id else "○"
        label = f"{prefix} {name}"
        # "menu:s:<id>" — ids are expected to be short DB slugs like "balanced"
        data = f"menu:s:{sid}"
        if len(data.encode()) > 64:
            data = f"menu:s:{sid[:56]}"
        keyboard.append([_btn(label, data)])

    keyboard.append([_back_btn("main"), _btn("⚙ Settings", f"menu:se:{active_id}")])
    return InlineKeyboardMarkup(keyboard)


def build_strategy_detail_menu(strategy: dict) -> InlineKeyboardMarkup:
    """
    Buttons for tunable params: [TP1 30%] [SL -25%] etc.
    Last two rows: [Activate this] [Back].

    strategy: dict with at least {"id": str, "name": str, "config": dict}.
    """
    sid = strategy.get("id", "")
    config: dict = strategy.get("config") or {}

    # Param display: (label_format, config_key)
    param_rows: list[tuple[str, str]] = [
        ("TP1 +{tp1_gain_pct}%",  "tp1_gain_pct"),
        ("TP2 +{tp2_gain_pct}%",  "tp2_gain_pct"),
        ("TP3 +{tp3_gain_pct}%",  "tp3_gain_pct"),
        ("SL {hard_sl_pct}%",     "hard_sl_pct"),
        ("Trail {trailing_stop_pct}%", "trailing_stop_pct"),
        ("MinScore {min_score_to_buy}", "min_score_to_buy"),
    ]

    keyboard: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    for tmpl, key in param_rows:
        val = config.get(key, "?")
        label = tmpl.format(**{key: val})
        # param edit callback
        data = f"menu:se:{sid}:{key}"
        if len(data.encode()) > 64:
            data = f"menu:se:{sid[:30]}:{key[:20]}"
        row.append(_btn(label, data))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    # Action row
    keyboard.append([
        _btn("✅ Activate this", f"menu:sa:{sid}"),
        _back_btn("strategy"),
    ])
    return InlineKeyboardMarkup(keyboard)


def build_positions_menu(
    positions: list[dict],
    page: int = 0,
) -> InlineKeyboardMarkup:
    """
    One row per position: [SYMBOL +12.3%].  Paginated at 8 items.
    Last row: [Back] (and [Next ▶] / [◀ Prev] if paginated).

    positions: list of dicts with at least:
        {"db_id": int, "token_symbol": str, "gain_pct": float}
    """
    if not positions:
        keyboard = [[_btn("(no open positions)", "menu:bk:main")]]
        keyboard.append([_back_btn("main")])
        return InlineKeyboardMarkup(keyboard)

    start = page * _PAGE_SIZE
    page_items = positions[start : start + _PAGE_SIZE]

    keyboard: list[list[InlineKeyboardButton]] = []
    for pos in page_items:
        db_id = pos.get("db_id", 0)
        sym = pos.get("token_symbol", "???")[:10]
        gain = pos.get("gain_pct", 0.0)
        sign = "+" if gain >= 0 else ""
        label = f"{sym} {sign}{gain:.1f}%"
        keyboard.append([_btn(label, f"menu:p:{db_id}")])

    nav: list[InlineKeyboardButton] = [_back_btn("main")]
    if start > 0:
        nav.insert(0, _btn("◀ Prev", f"menu:m:positions:{page - 1}"))
    if start + _PAGE_SIZE < len(positions):
        nav.append(_btn("Next ▶", f"menu:m:positions:{page + 1}"))
    keyboard.append(nav)

    return InlineKeyboardMarkup(keyboard)


def build_position_detail_menu(position: dict) -> InlineKeyboardMarkup:
    """
    Sell controls for a single position.

    position: dict with at least {"db_id": int, "token_symbol": str}.
    """
    db_id = position.get("db_id", 0)
    keyboard = [
        [
            _btn("Sell 25%",  f"menu:ps:25:{db_id}"),
            _btn("Sell 50%",  f"menu:ps:50:{db_id}"),
            _btn("Sell 100%", f"menu:ps:100:{db_id}"),
        ],
        [
            _btn("Force TP", f"menu:ps:tp:{db_id}"),
            _btn("Force SL", f"menu:ps:sl:{db_id}"),
        ],
        [_back_btn("positions")],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_alerts_menu(
    alerts: list[dict],
    page: int = 0,
) -> InlineKeyboardMarkup:
    """
    Pending price alerts.  Each row: [SYMBOL @ target  ❌ Cancel].
    Paginated at 8 items.

    alerts: list of dicts with at least:
        {"id": int, "symbol": str, "alert_type": str,
         "target_price_usd": float|None, "target_ath_distance_pct": float|None}
    """
    if not alerts:
        keyboard = [[_btn("(no pending alerts)", "menu:bk:main")]]
        keyboard.append([_back_btn("main")])
        return InlineKeyboardMarkup(keyboard)

    start = page * _PAGE_SIZE
    page_items = alerts[start : start + _PAGE_SIZE]

    keyboard: list[list[InlineKeyboardButton]] = []
    for al in page_items:
        alert_id = al.get("id", 0)
        sym = al.get("symbol", "???")[:8]
        atype = al.get("alert_type", "")
        if atype == "dip_target":
            target = al.get("target_price_usd")
            tgt_str = f"${target:.4f}" if target else "?"
        else:
            pct = al.get("target_ath_distance_pct")
            tgt_str = f"{pct:.0f}%ATH" if pct else "?"
        label = f"{sym} {tgt_str}"
        row = [
            _btn(label, f"menu:p:{alert_id}"),
            _btn("❌", f"menu:ac:{alert_id}"),
        ]
        keyboard.append(row)

    nav: list[InlineKeyboardButton] = [_back_btn("main")]
    if start > 0:
        nav.insert(0, _btn("◀ Prev", f"menu:m:alerts:{page - 1}"))
    if start + _PAGE_SIZE < len(alerts):
        nav.append(_btn("Next ▶", f"menu:m:alerts:{page + 1}"))
    keyboard.append(nav)

    return InlineKeyboardMarkup(keyboard)


def build_settings_menu(settings_dict: dict) -> InlineKeyboardMarkup:
    """
    Current values for key params with [Edit] affordance (info-only for now;
    edit flow is handled by strategy detail menu).

    settings_dict: dict of param_name → current_value.
    """
    display_keys = [
        ("min_score_to_buy",      "MinScore"),
        ("max_position_size_sol", "MaxPos SOL"),
        ("hard_sl_pct",           "Hard SL"),
        ("tp1_gain_pct",          "TP1 gain"),
        ("slippage_bps",          "Slippage bps"),
        ("max_concurrent_positions", "Max conc."),
    ]

    keyboard: list[list[InlineKeyboardButton]] = []
    for key, label in display_keys:
        val = settings_dict.get(key, "?")
        row_label = f"{label}: {val}"
        keyboard.append([
            _btn(row_label, f"menu:se:view:{key}"),
            _btn("Edit", f"menu:se:edit:{key}"),
        ])

    keyboard.append([_back_btn("main")])
    return InlineKeyboardMarkup(keyboard)


def build_confirm_intent_menu(intent_id: str) -> InlineKeyboardMarkup:
    """
    [✅ Approve] [❌ Reject] for confirm-mode trade intents.

    intent_id: short unique ID for the pending trade.
    """
    # Keep intent_id short enough; truncate at 48 chars to stay under 64 total
    safe_id = intent_id[:48]
    keyboard = [
        [
            _btn("✅ Approve", f"menu:ci:ok:{safe_id}"),
            _btn("❌ Reject",  f"menu:ci:no:{safe_id}"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def build_back_menu(back_to: str = "main") -> InlineKeyboardMarkup:
    """Single [« Back] button row."""
    return InlineKeyboardMarkup([[_back_btn(back_to)]])
