"""
Phase 10: Dip-Buy Entry Mode Resolver.

Decides — given a scored candidate and the active strategy config — whether
the bot should:

  IMMEDIATE   — buy right now (entry_mode == "immediate", or dip target
                already met)
  STORE_ALERT — price is not yet at target; park in price_alerts table
  SKIP        — strategy disables dip-buy or required data is unavailable

Called by SignalEngine (which I will wire in separately).

Entry modes supported by this resolver
---------------------------------------
immediate
    Always IMMEDIATE; this function is effectively a no-op pass-through.

wait_for_dip
    "Price must drop X% from the detection-moment price."
    target_price = current_price * (1 + max_ath_distance_pct / 100)
    If current price is already at or below that level → IMMEDIATE.
    Otherwise → STORE_ALERT with alert_type='dip_target'.

wait_for_dump
    "Price must be at least X% below the ATH seen since launch."
    ATH comes from:
      1. token_data.price_ath  (filled by scorer from OHLC)
      2. Fallback: gecko OHLC last 24 h max-high
      3. Final fallback: current price treated as ATH
    If the ATH-distance is already ≤ target → IMMEDIATE.
    Otherwise → STORE_ALERT with alert_type='dump_from_ath'.

Notes
-----
- This module does *not* do any DB writes itself; it returns a decision
  string and the caller is responsible for invoking alert_manager.store_alert.
- gecko OHLC is only fetched for wait_for_dump when token_data.price_ath == 0.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.infra.logger import get_logger

if TYPE_CHECKING:
    from src.clients.geckoterminal import GeckoTerminalClient
    from src.core.price_alerts import PriceAlertManager
    from src.core.scoring import TokenData

log = get_logger(__name__)

# Return values
IMMEDIATE = "IMMEDIATE"
STORE_ALERT = "STORE_ALERT"
SKIP = "SKIP"


async def resolve_entry_mode(
    candidate: dict,
    token_data: "TokenData",
    strategy: dict,
    alert_manager: "PriceAlertManager",
) -> str:
    """
    Evaluate whether to buy immediately, queue an alert, or skip.

    Parameters
    ----------
    candidate:
        Raw scanner dict (mint, symbol, price_usd, …).
    token_data:
        Enriched + scored TokenData object.
    strategy:
        Active strategy config dict (from StrategyManager.get_active()).
    alert_manager:
        PriceAlertManager instance (used only to fetch gecko for ATH when
        token_data.price_ath is unavailable).

    Returns
    -------
    "IMMEDIATE" | "STORE_ALERT" | "SKIP"
    """
    entry_mode: str = strategy.get("entry_mode", "immediate")
    current_price: float = float(token_data.price_usd or candidate.get("price_usd", 0))
    mint: str = token_data.address
    symbol: str = token_data.symbol or candidate.get("symbol", "")
    strategy_id: str = strategy.get("id", "unknown")

    if current_price <= 0:
        log.warning("dip_resolver_no_price", mint=mint)
        return SKIP

    # ------------------------------------------------------------------ #
    # 1. IMMEDIATE — no waiting
    # ------------------------------------------------------------------ #
    if entry_mode == "immediate":
        return IMMEDIATE

    # ------------------------------------------------------------------ #
    # 2. wait_for_dip — drop X% from detection-moment price
    # ------------------------------------------------------------------ #
    if entry_mode == "wait_for_dip":
        target_pct: float = float(strategy.get("max_ath_distance_pct", -10))
        # target_price is current_price * (1 + target_pct/100)
        # e.g. target_pct=-10, current=1.0 → target=0.90
        target_price = current_price * (1 + target_pct / 100)

        if current_price <= target_price:
            log.debug(
                "dip_resolver_already_at_target",
                mode=entry_mode,
                mint=mint,
                current=current_price,
                target=target_price,
            )
            return IMMEDIATE

        # Need to wait
        await alert_manager.store_alert(
            mint=mint,
            symbol=symbol,
            strategy_id=strategy_id,
            alert_type="dip_target",
            target_price_usd=target_price,
            target_ath_distance_pct=None,
            ath_price_seen_usd=None,
            signal_data=candidate,
        )
        log.info(
            "dip_resolver_alert_stored",
            mode=entry_mode,
            mint=mint,
            current=current_price,
            target=target_price,
        )
        return STORE_ALERT

    # ------------------------------------------------------------------ #
    # 3. wait_for_dump — drop X% below ATH since launch
    # ------------------------------------------------------------------ #
    if entry_mode == "wait_for_dump":
        target_ath_pct: float = float(strategy.get("max_ath_distance_pct", -80))
        ath_price = await _resolve_ath(token_data, alert_manager)

        if ath_price <= 0:
            log.warning("dip_resolver_no_ath", mint=mint)
            return SKIP

        distance_pct = (current_price - ath_price) / ath_price * 100

        if distance_pct <= target_ath_pct:
            log.debug(
                "dip_resolver_already_at_target",
                mode=entry_mode,
                mint=mint,
                distance_pct=distance_pct,
                target_pct=target_ath_pct,
            )
            return IMMEDIATE

        # Store alert: wait for the dump
        await alert_manager.store_alert(
            mint=mint,
            symbol=symbol,
            strategy_id=strategy_id,
            alert_type="dump_from_ath",
            target_price_usd=None,
            target_ath_distance_pct=target_ath_pct,
            ath_price_seen_usd=ath_price,
            signal_data=candidate,
        )
        log.info(
            "dip_resolver_alert_stored",
            mode=entry_mode,
            mint=mint,
            ath=ath_price,
            current=current_price,
            distance_pct=distance_pct,
            target_pct=target_ath_pct,
        )
        return STORE_ALERT

    # ------------------------------------------------------------------ #
    # Unknown entry_mode — fail safe
    # ------------------------------------------------------------------ #
    log.warning("dip_resolver_unknown_mode", entry_mode=entry_mode, mint=mint)
    return SKIP


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _resolve_ath(
    token_data: "TokenData",
    alert_manager: "PriceAlertManager",
) -> float:
    """
    Determine best-available ATH for wait_for_dump logic.

    Priority:
      1. token_data.price_ath  (pre-filled by scorer from OHLC)
      2. gecko OHLC last 24h max-high  (fetched lazily)
      3. current price (treated as ATH — means target can never be hit
         unless price rises then falls, so we still store the alert)
    """
    if token_data.price_ath and token_data.price_ath > 0:
        return float(token_data.price_ath)

    # Try gecko OHLC
    try:
        gecko = alert_manager.gecko
        candles = await gecko.get_token_ohlcv(
            token_data.address,
            timeframe="hour",
            aggregate=1,
            limit=24,
        )
        if candles:
            # Each candle: [ts, open, high, low, close, volume]
            max_high = max(float(c[2]) for c in candles if len(c) >= 4)
            if max_high > 0:
                return max_high
    except Exception as exc:
        log.debug(
            "dip_resolver_ath_gecko_failed",
            mint=token_data.address,
            error=str(exc),
        )

    # Final fallback: treat current price as ATH
    return float(token_data.price_usd or 0)
