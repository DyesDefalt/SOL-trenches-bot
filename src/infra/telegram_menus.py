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
    Sell controls + Phase 11.1 quick-action overrides for a single position.

    position: dict with {"db_id": int, "token_symbol": str, "trail_active": bool}.

    Layout:
      Row 1: [Sell 25%] [Sell 50%] [Sell 100%]
      Row 2: [TP +25%] [TP +50%]                      ← Phase 11.1 override TP1
      Row 3: [SL -15%] [SL -25%]                      ← Phase 11.1 override SL
      Row 4: [Trail ON/OFF] [🔄 Refresh]              ← Phase 11.1 trail toggle
      Row 5: [« Back]
    """
    db_id = position.get("db_id", 0)
    trail_active = position.get("trail_active", True)
    trail_label = "Trail ✓ ON" if trail_active else "Trail ○ OFF"
    keyboard = [
        [
            _btn("Sell 25%",  f"menu:ps:25:{db_id}"),
            _btn("Sell 50%",  f"menu:ps:50:{db_id}"),
            _btn("Sell 100%", f"menu:ps:100:{db_id}"),
        ],
        [
            _btn("TP +25%", f"menu:po:tp:25:{db_id}"),
            _btn("TP +50%", f"menu:po:tp:50:{db_id}"),
        ],
        [
            _btn("SL -15%", f"menu:po:sl:-15:{db_id}"),
            _btn("SL -25%", f"menu:po:sl:-25:{db_id}"),
        ],
        [
            _btn(trail_label,  f"menu:po:trail:{db_id}"),
            _btn("🔄 Refresh", f"menu:po:refresh:{db_id}"),
        ],
        [_back_btn("positions")],
    ]
    return InlineKeyboardMarkup(keyboard)


def format_position_card(position: dict) -> str:
    """
    Phase 11.3: Rich text card for a single position (HTML formatted for Telegram).

    position: dict from PositionManager.get_open_positions_summary() — keys include:
      symbol, entry_price_usd, current_price_usd, peak_price_usd, gain_pct, size_sol,
      tp_active_pct, sl_active_pct, trail_active, tp_override, sl_override,
      buy_pressure_pct, vol_liq_ratio, rug_score, liquidity_usd, mcap_usd
    """
    sym = position.get("symbol") or position.get("token_address", "?")[:8]
    entry = position.get("entry_price_usd") or 0
    cur = position.get("current_price_usd") or entry
    peak = position.get("peak_price_usd") or entry
    gain = position.get("gain_pct")
    size = position.get("size_sol") or 0

    # PnL line
    if gain is not None:
        gain_emoji = "🟢" if gain >= 0 else "🔴"
        gain_str = f"{gain_emoji} <b>{gain:+.2f}%</b>"
    else:
        gain_str = "<i>price loading…</i>"

    # Multiple-of-entry display (1.5x, 3.36x etc.)
    mult = cur / entry if entry > 0 else 1.0
    peak_mult = peak / entry if entry > 0 else 1.0

    # SL distance line
    sl_pct = position.get("sl_active_pct", -25)
    sl_price = entry * (1 + sl_pct / 100) if entry > 0 else 0
    sl_distance_pct = ((cur - sl_price) / cur * 100) if cur > 0 else 0
    sl_override_tag = " <code>[OVR]</code>" if position.get("sl_override") is not None else ""

    # TP line
    tp_pct = position.get("tp_active_pct", 80)
    tp_override_tag = " <code>[OVR]</code>" if position.get("tp_override") is not None else ""
    tp_done = []
    if position.get("tp1_done"): tp_done.append("TP1")
    if position.get("tp2_done"): tp_done.append("TP2")
    if position.get("tp3_done"): tp_done.append("TP3")
    tp_done_str = f" ({'+'.join(tp_done)} done)" if tp_done else ""

    # Trail
    trail_str = "✓ ON" if position.get("trail_active") else "○ OFF"

    # Metrics line (only show what we have)
    metrics_parts = []
    if position.get("liquidity_usd"):
        metrics_parts.append(f"Liq ${position['liquidity_usd'] / 1000:.1f}K")
    if position.get("mcap_usd"):
        metrics_parts.append(f"MCap ${position['mcap_usd'] / 1000:.1f}K")
    if position.get("vol_liq_ratio") is not None:
        metrics_parts.append(f"V/L {position['vol_liq_ratio']:.1f}x")
    if position.get("buy_pressure_pct") is not None:
        metrics_parts.append(f"BuyP {position['buy_pressure_pct']:.0f}%")
    if position.get("rug_score") is not None:
        metrics_parts.append(f"Rug {position['rug_score']}")
    metrics_line = " · ".join(metrics_parts) if metrics_parts else ""

    lines = [
        f"<b>{sym}</b>  {gain_str}",
        f"<code>Size:    {size:.4f} SOL</code>",
        f"<code>Entry:   ${entry:.7f}</code>",
        f"<code>Current: ${cur:.7f}  ({mult:.2f}x)</code>",
        f"<code>Peak:    ${peak:.7f}  ({peak_mult:.2f}x)</code>",
        f"<code>SL:      ${sl_price:.7f}  ({sl_pct:+.0f}%, {sl_distance_pct:+.1f}% away){sl_override_tag}</code>",
        f"<code>TP1:     +{tp_pct:.0f}%{tp_done_str}{tp_override_tag}</code>",
        f"<code>Trail:   {trail_str}</code>",
    ]
    if metrics_line:
        lines.append(f"<code>{metrics_line}</code>")
    return "\n".join(lines)


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
