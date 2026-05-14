"""
Phase 10: Dip-Buy Price Alert Manager.

Stores candidate tokens that scored well but aren't yet at their target
entry price. A background polling loop checks live prices and fires the
on_trigger_callback when conditions are met.

Two alert types:
- dip_target      — trigger when price ≤ target_price_usd
                    (drop from detection-moment price)
- dump_from_ath   — trigger when ((current - ath) / ath * 100) ≤
                    target_ath_distance_pct
                    (e.g. -85 ≤ -80 means 85% below ATH)

Concurrency:
    SELECT … FOR UPDATE SKIP LOCKED is used in check_pending() so that
    multiple workers (or asyncio tasks reusing the same event loop) cannot
    double-fire the same alert. Each row is locked for the duration of the
    status update, then released.

Stale-price handling:
    If gecko fails to return a price for a mint the alert is skipped for
    that cycle (not marked expired). The expiry clock keeps ticking, so
    a persistently-stale token will eventually be swept by the expiry path.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Awaitable

from src.infra.logger import get_logger

if False:  # TYPE_CHECKING avoidance for circular imports at runtime
    from src.clients.geckoterminal import GeckoTerminalClient
    from src.infra.db import Database

log = get_logger(__name__)

_MS_PER_HOUR = 3_600_000
_7_DAYS_MS = 7 * 24 * _MS_PER_HOUR
_24H_MS = 24 * _MS_PER_HOUR

TriggerCallback = Callable[[str, dict], Awaitable[None]]


class PriceAlertManager:
    """Manage dip-buy price alerts with persistent DB backing."""

    def __init__(
        self,
        db: "Database",
        gecko: "GeckoTerminalClient",
        on_trigger_callback: TriggerCallback,
    ) -> None:
        self.db = db
        self.gecko = gecko
        self._callback = on_trigger_callback

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def store_alert(
        self,
        mint: str,
        symbol: str,
        strategy_id: str,
        alert_type: str,
        target_price_usd: float | None,
        target_ath_distance_pct: float | None,
        ath_price_seen_usd: float | None,
        signal_data: dict,
        expires_ms: int = 86_400_000,  # 24 h default
    ) -> int:
        """
        Persist a new alert. Returns the auto-generated row id.

        alert_type must be 'dip_target' or 'dump_from_ath'.
        expires_ms is relative (added to current epoch-ms).
        """
        if alert_type not in ("dip_target", "dump_from_ath"):
            raise ValueError(f"Unknown alert_type: {alert_type!r}")

        now_ms = _now_ms()
        assert self.db._pool is not None
        async with self.db._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO price_alerts (
                    mint, symbol, strategy_id, alert_type,
                    target_price_usd, target_ath_distance_pct, ath_price_seen_usd,
                    detected_at_ms, expires_at_ms, signal_data, status
                ) VALUES (
                    $1, $2, $3, $4,
                    $5, $6, $7,
                    $8, $9, $10, 'pending'
                ) RETURNING id
                """,
                mint,
                symbol,
                strategy_id,
                alert_type,
                target_price_usd,
                target_ath_distance_pct,
                ath_price_seen_usd,
                now_ms,
                now_ms + expires_ms,
                json.dumps(signal_data),
            )
            alert_id: int = row["id"]
            log.info(
                "price_alert_stored",
                id=alert_id,
                mint=mint,
                type=alert_type,
                target_price=target_price_usd,
                target_ath_pct=target_ath_distance_pct,
            )
            return alert_id

    async def check_pending(self) -> int:
        """
        Poll all pending alerts. For each:
        - Fetch current price via gecko.
        - If price condition met → call callback, mark 'triggered'.
        - If expired → mark 'expired'.
        - If gecko fails → skip (keep 'pending').

        Uses SELECT FOR UPDATE SKIP LOCKED to prevent double-fire when
        multiple workers are running concurrently.

        Returns the count of alerts triggered this cycle.
        """
        assert self.db._pool is not None
        now_ms = _now_ms()
        triggered_count = 0

        async with self.db._pool.acquire() as conn:
            async with conn.transaction():
                rows = await conn.fetch(
                    """
                    SELECT id, mint, symbol, strategy_id, alert_type,
                           target_price_usd, target_ath_distance_pct,
                           ath_price_seen_usd, expires_at_ms, signal_data
                    FROM price_alerts
                    WHERE status = 'pending'
                    ORDER BY id
                    FOR UPDATE SKIP LOCKED
                    """
                )

                for row in rows:
                    alert = dict(row)
                    alert_id: int = alert["id"]
                    mint: str = alert["mint"]

                    # --- Expiry check (no price fetch needed) ---
                    if now_ms >= alert["expires_at_ms"]:
                        await conn.execute(
                            "UPDATE price_alerts SET status='expired' WHERE id=$1",
                            alert_id,
                        )
                        log.info("price_alert_expired", id=alert_id, mint=mint)
                        continue

                    # --- Fetch current price ---
                    current_price = await self._fetch_price(mint)
                    if current_price is None:
                        # Stale/missing — skip this cycle, keep pending
                        log.debug(
                            "price_alert_price_unavailable",
                            id=alert_id,
                            mint=mint,
                        )
                        continue

                    # --- Trigger evaluation ---
                    signal_data: dict = (
                        json.loads(alert["signal_data"])
                        if isinstance(alert["signal_data"], str)
                        else dict(alert["signal_data"])
                    )

                    if self._should_trigger(alert, current_price):
                        await conn.execute(
                            """
                            UPDATE price_alerts
                            SET status='triggered', triggered_at_ms=$2
                            WHERE id=$1
                            """,
                            alert_id,
                            now_ms,
                        )
                        triggered_count += 1
                        log.info(
                            "price_alert_triggered",
                            id=alert_id,
                            mint=mint,
                            current_price=current_price,
                        )
                        # Fire callback outside transaction lock to avoid
                        # holding the row lock during potentially slow work.
                        # We've already written 'triggered' so idempotent.
                        try:
                            await self._callback(mint, signal_data)
                        except Exception as exc:
                            log.error(
                                "price_alert_callback_failed",
                                id=alert_id,
                                mint=mint,
                                error=str(exc),
                            )

        return triggered_count

    async def cancel_alert(self, alert_id: int) -> bool:
        """
        Mark an alert as cancelled. Returns True if a pending alert was found
        and updated, False if not found or already in terminal state.
        """
        assert self.db._pool is not None
        async with self.db._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE price_alerts
                SET status='cancelled'
                WHERE id=$1 AND status='pending'
                """,
                alert_id,
            )
            # asyncpg returns "UPDATE N" tag string
            updated = int(result.split()[-1]) > 0
            if updated:
                log.info("price_alert_cancelled", id=alert_id)
            return updated

    async def list_pending(self, strategy_id: str | None = None) -> list[dict[str, Any]]:
        """
        Return pending alerts, optionally filtered by strategy.

        Used by the Telegram /alerts command.
        """
        assert self.db._pool is not None
        async with self.db._pool.acquire() as conn:
            if strategy_id:
                rows = await conn.fetch(
                    """
                    SELECT id, mint, symbol, strategy_id, alert_type,
                           target_price_usd, target_ath_distance_pct,
                           ath_price_seen_usd, detected_at_ms, expires_at_ms
                    FROM price_alerts
                    WHERE status='pending' AND strategy_id=$1
                    ORDER BY detected_at_ms DESC
                    """,
                    strategy_id,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, mint, symbol, strategy_id, alert_type,
                           target_price_usd, target_ath_distance_pct,
                           ath_price_seen_usd, detected_at_ms, expires_at_ms
                    FROM price_alerts
                    WHERE status='pending'
                    ORDER BY detected_at_ms DESC
                    """
                )
            return [dict(r) for r in rows]

    async def cleanup_expired(self) -> int:
        """
        Hard-delete triggered/expired alerts older than 7 days.

        Returns count of rows deleted.
        """
        assert self.db._pool is not None
        cutoff_ms = _now_ms() - _7_DAYS_MS
        async with self.db._pool.acquire() as conn:
            result = await conn.execute(
                """
                DELETE FROM price_alerts
                WHERE status IN ('expired', 'triggered', 'cancelled')
                  AND detected_at_ms < $1
                """,
                cutoff_ms,
            )
            deleted = int(result.split()[-1])
            if deleted:
                log.info("price_alerts_cleanup", deleted=deleted)
            return deleted

    async def get_stats(self) -> dict[str, int]:
        """Return {pending, triggered_24h, expired_24h} counts."""
        assert self.db._pool is not None
        cutoff_ms = _now_ms() - _24H_MS
        async with self.db._pool.acquire() as conn:
            pending = await conn.fetchval(
                "SELECT COUNT(*) FROM price_alerts WHERE status='pending'"
            ) or 0
            triggered_24h = await conn.fetchval(
                """
                SELECT COUNT(*) FROM price_alerts
                WHERE status='triggered' AND triggered_at_ms >= $1
                """,
                cutoff_ms,
            ) or 0
            expired_24h = await conn.fetchval(
                """
                SELECT COUNT(*) FROM price_alerts
                WHERE status='expired' AND detected_at_ms >= $1
                """,
                cutoff_ms,
            ) or 0
        return {
            "pending": int(pending),
            "triggered_24h": int(triggered_24h),
            "expired_24h": int(expired_24h),
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _should_trigger(self, alert: dict, current_price_usd: float) -> bool:
        """
        Pure trigger evaluation — no I/O.

        dip_target:
            Trigger when current_price <= target_price_usd.

        dump_from_ath:
            Compute distance_pct = (current - ath) / ath * 100.
            Trigger when distance_pct <= target_ath_distance_pct.
            Example: distance_pct = -85, target = -80 → -85 <= -80 → True.
        """
        alert_type = alert["alert_type"]

        if alert_type == "dip_target":
            target = alert.get("target_price_usd")
            if target is None:
                return False
            return current_price_usd <= float(target)

        if alert_type == "dump_from_ath":
            ath = alert.get("ath_price_seen_usd")
            target_pct = alert.get("target_ath_distance_pct")
            if ath is None or target_pct is None or float(ath) <= 0:
                return False
            distance_pct = (current_price_usd - float(ath)) / float(ath) * 100
            return distance_pct <= float(target_pct)

        log.warning("price_alert_unknown_type", alert_type=alert_type)
        return False

    async def _fetch_price(self, mint: str) -> float | None:
        """
        Fetch current USD price for mint via gecko.

        Returns None on any error or if price is zero/missing, so the
        caller can skip the alert without marking it failed.
        """
        try:
            token_data = await self.gecko.get_token(mint)
            raw = token_data.get("attributes", {}).get("price_usd")
            if raw is None:
                return None
            price = float(raw)
            return price if price > 0 else None
        except Exception as exc:
            log.debug("price_alert_gecko_error", mint=mint, error=str(exc))
            return None


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _now_ms() -> int:
    """Current epoch time in milliseconds."""
    return int(time.time() * 1000)
