"""
PostgreSQL async DB layer via asyncpg.

Schema: lihat migrations/001_initial.sql

Tables:
- positions: open + closed positions, entry/exit data, realized PnL
- position_partial_exits: TP staircase tracking
- signals: every scoring decision
- smart_wallet_snapshots: periodic registry snapshot untuk audit
- circuit_breaker_events: when bot paused/halted + state
- daily_pnl: aggregated per-day PnL (untuk Telegram /pnl calendar)
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

import asyncpg

from src.config import settings
from src.infra.logger import get_logger

log = get_logger(__name__)


class Database:
    """Async Postgres client wrapper."""

    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = dsn or settings.postgres_dsn
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        """Initialize connection pool."""
        if self._pool is not None:
            return
        try:
            self._pool = await asyncpg.create_pool(
                self.dsn,
                min_size=2,
                max_size=10,
                command_timeout=30,
            )
            # Test
            async with self._pool.acquire() as conn:
                await conn.execute("SELECT 1")
            log.info("db_connected")
        except Exception as e:
            log.error("db_connect_failed", error=str(e))
            raise

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None

    async def __aenter__(self) -> "Database":
        await self.connect()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Schema management
    # ------------------------------------------------------------------
    async def init_schema(self, migration_path: str = "migrations/001_initial.sql") -> None:
        """Run initial migration. Idempotent (CREATE IF NOT EXISTS)."""
        from pathlib import Path

        sql = Path(migration_path).read_text()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(sql)
        log.info("db_schema_initialized")

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------
    async def insert_position(
        self,
        token_address: str,
        token_symbol: str,
        entry_price_usd: float,
        entry_amount_sol: float,
        entry_amount_token: float,
        entry_signature: str,
        entry_score: float,
        entry_smart_money_count: int,
        token_name: str = "",
        dry_run: bool = False,
    ) -> int:
        """Insert new open position. Returns position id."""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO positions (
                    token_address, token_symbol, token_name,
                    entry_price_usd, entry_amount_sol, entry_amount_token,
                    entry_signature, entry_score, entry_smart_money_count,
                    peak_price_usd, dry_run, status
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $4, $10, 'OPEN'
                ) RETURNING id
                """,
                token_address,
                token_symbol,
                token_name,
                entry_price_usd,
                entry_amount_sol,
                entry_amount_token,
                entry_signature,
                entry_score,
                entry_smart_money_count,
                dry_run,
            )
            return row["id"]

    async def update_position_peak(self, position_id: int, peak_price_usd: float) -> None:
        """Update peak price (untuk trailing stop calculation)."""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE positions SET peak_price_usd = GREATEST(peak_price_usd, $1)
                WHERE id = $2
                """,
                peak_price_usd,
                position_id,
            )

    async def update_position_override(
        self,
        position_id: int,
        tp_pct: float | None = None,
        sl_pct: float | None = None,
        trail_disabled: bool | None = None,
        set_by: str = "telegram",
    ) -> None:
        """Phase 11.1: persist per-position TP/SL/trail override (idempotent)."""
        assert self._pool is not None
        # Build UPDATE dynamically — only set fields that are provided
        sets: list[str] = []
        args: list[Any] = []
        idx = 1
        if tp_pct is not None:
            sets.append(f"tp_override_pct = ${idx}")
            args.append(float(tp_pct))
            idx += 1
        if sl_pct is not None:
            sets.append(f"sl_override_pct = ${idx}")
            args.append(float(sl_pct))
            idx += 1
        if trail_disabled is not None:
            sets.append(f"trail_disabled = ${idx}")
            args.append(bool(trail_disabled))
            idx += 1
        if not sets:
            return  # nothing to update
        sets.append(f"override_set_at_ms = ${idx}")
        args.append(int(__import__("time").time() * 1000))
        idx += 1
        sets.append(f"override_set_by = ${idx}")
        args.append(str(set_by))
        idx += 1
        sets.append(f"updated_at = NOW()")
        args.append(int(position_id))
        sql = f"UPDATE positions SET {', '.join(sets)} WHERE id = ${idx}"
        async with self._pool.acquire() as conn:
            await conn.execute(sql, *args)

    async def get_pnl_breakdown_by_exit_reason(
        self,
        days: int = 30,
        dry_run: bool | None = None,
    ) -> list[dict[str, Any]]:
        """
        Phase 11.2: group closed positions by exit_reason with count + avg PnL + total PnL.

        Returns: [{exit_reason, count, avg_pnl_pct, total_pnl_sol, avg_hold_minutes}, ...]
        """
        assert self._pool is not None
        where_dry = "AND dry_run = $2" if dry_run is not None else ""
        params: list[Any] = [days]
        if dry_run is not None:
            params.append(dry_run)
        sql = f"""
            SELECT
                exit_reason,
                COUNT(*)::int AS count,
                ROUND(AVG(realized_pnl_pct)::numeric, 2)::float AS avg_pnl_pct,
                ROUND(SUM(realized_pnl_sol)::numeric, 6)::float AS total_pnl_sol,
                ROUND(AVG(EXTRACT(EPOCH FROM (exit_timestamp - entry_timestamp)) / 60)::numeric, 1)::float AS avg_hold_minutes
            FROM positions
            WHERE status = 'CLOSED'
              AND exit_timestamp >= NOW() - ($1::int * INTERVAL '1 day')
              AND exit_reason IS NOT NULL
              {where_dry}
            GROUP BY exit_reason
            ORDER BY count DESC
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
            return [dict(row) for row in rows]

    async def get_best_worst_trades(
        self,
        days: int = 30,
        limit: int = 1,
    ) -> dict[str, list[dict[str, Any]]]:
        """Phase 11.2: top N best + worst closed trades by realized_pnl_pct."""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            best = await conn.fetch(
                """
                SELECT token_symbol, token_address, realized_pnl_pct, realized_pnl_sol,
                       exit_reason,
                       EXTRACT(EPOCH FROM (exit_timestamp - entry_timestamp)) / 60 AS hold_minutes
                FROM positions
                WHERE status = 'CLOSED'
                  AND exit_timestamp >= NOW() - ($1::int * INTERVAL '1 day')
                  AND realized_pnl_pct IS NOT NULL
                ORDER BY realized_pnl_pct DESC NULLS LAST
                LIMIT $2
                """,
                days, limit,
            )
            worst = await conn.fetch(
                """
                SELECT token_symbol, token_address, realized_pnl_pct, realized_pnl_sol,
                       exit_reason,
                       EXTRACT(EPOCH FROM (exit_timestamp - entry_timestamp)) / 60 AS hold_minutes
                FROM positions
                WHERE status = 'CLOSED'
                  AND exit_timestamp >= NOW() - ($1::int * INTERVAL '1 day')
                  AND realized_pnl_pct IS NOT NULL
                ORDER BY realized_pnl_pct ASC NULLS LAST
                LIMIT $2
                """,
                days, limit,
            )
            return {
                "best": [dict(row) for row in best],
                "worst": [dict(row) for row in worst],
            }

    async def close_position(
        self,
        position_id: int,
        exit_price_usd: float,
        exit_amount_sol: float,
        exit_signature: str,
        exit_reason: str,
        realized_pnl_sol: float,
        realized_pnl_pct: float,
        status: str = "CLOSED",
    ) -> None:
        """Close position dengan exit data + PnL."""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE positions SET
                    status = $2,
                    exit_price_usd = $3,
                    exit_amount_sol = $4,
                    exit_signature = $5,
                    exit_reason = $6,
                    exit_timestamp = NOW(),
                    realized_pnl_sol = $7,
                    realized_pnl_pct = $8
                WHERE id = $1
                """,
                position_id,
                status,
                exit_price_usd,
                exit_amount_sol,
                exit_signature,
                exit_reason,
                realized_pnl_sol,
                realized_pnl_pct,
            )

    async def add_partial_exit(
        self,
        position_id: int,
        tier: str,
        sell_price_usd: float,
        sell_amount_token: float,
        sell_amount_sol: float,
        signature: str,
        pnl_pct: float,
    ) -> None:
        """Record partial exit (TP1/TP2/TP3/TRAILING)."""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO position_partial_exits (
                    position_id, tier, sell_price_usd, sell_amount_token,
                    sell_amount_sol, signature, pnl_pct
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                position_id,
                tier,
                sell_price_usd,
                sell_amount_token,
                sell_amount_sol,
                signature,
                pnl_pct,
            )

    async def get_open_positions(self) -> list[dict[str, Any]]:
        """All currently open positions."""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM positions WHERE status = 'OPEN'")
            return [dict(r) for r in rows]

    async def count_open_positions(self) -> int:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM positions WHERE status = 'OPEN'") or 0

    async def get_recent_closed_positions(self, limit: int = 20) -> list[dict[str, Any]]:
        """Recent N closed positions (untuk circuit breaker logic)."""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM positions
                WHERE status = 'CLOSED' AND exit_timestamp IS NOT NULL
                ORDER BY exit_timestamp DESC LIMIT $1
                """,
                limit,
            )
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------
    async def insert_signal(
        self,
        token_address: str,
        token_symbol: str,
        score: float,
        action: str,
        reject_reasons: list[str],
        breakdown: dict[str, Any],
        context: dict[str, Any],
        smart_money_count: int,
        smart_money_buyers: list[str],
    ) -> int:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO signals (
                    token_address, token_symbol, score, action,
                    reject_reasons, breakdown, context,
                    smart_money_count, smart_money_buyers
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING id
                """,
                token_address,
                token_symbol,
                score,
                action,
                json.dumps(reject_reasons),
                json.dumps(breakdown),
                json.dumps(context),
                smart_money_count,
                json.dumps(smart_money_buyers),
            )
            return row["id"]

    # ------------------------------------------------------------------
    # Circuit breaker
    # ------------------------------------------------------------------
    async def insert_cb_event(
        self,
        trigger_type: str,
        threshold_value: float,
        actual_value: float,
        paused_until: datetime | None,
        state_snapshot: dict[str, Any],
    ) -> int:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO circuit_breaker_events (
                    trigger_type, threshold_value, actual_value,
                    paused_until, state_snapshot
                ) VALUES ($1, $2, $3, $4, $5)
                RETURNING id
                """,
                trigger_type,
                threshold_value,
                actual_value,
                paused_until,
                json.dumps(state_snapshot),
            )
            return row["id"]

    async def get_active_cb_events(self) -> list[dict[str, Any]]:
        """Unresolved CB events (paused_until > now or never resolved)."""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM circuit_breaker_events
                WHERE resolved_at IS NULL
                  AND (paused_until IS NULL OR paused_until > NOW())
                ORDER BY timestamp DESC
                """
            )
            return [dict(r) for r in rows]

    async def resolve_cb_event(self, event_id: int, resolved_by: str = "auto") -> None:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE circuit_breaker_events
                SET resolved_at = NOW(), resolved_by = $2
                WHERE id = $1
                """,
                event_id,
                resolved_by,
            )

    # ------------------------------------------------------------------
    # Daily PnL
    # ------------------------------------------------------------------
    async def upsert_daily_pnl(
        self,
        date_value: date,
        delta_pnl_sol: float,
        won: bool,
        starting_balance: float | None = None,
        ending_balance: float | None = None,
    ) -> None:
        """Increment counters for the day."""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO daily_pnl (date, trades_total, trades_won, trades_lost, pnl_sol, starting_balance_sol, ending_balance_sol)
                VALUES ($1, 1, $2, $3, $4, $5, $6)
                ON CONFLICT (date) DO UPDATE SET
                    trades_total = daily_pnl.trades_total + 1,
                    trades_won = daily_pnl.trades_won + EXCLUDED.trades_won,
                    trades_lost = daily_pnl.trades_lost + EXCLUDED.trades_lost,
                    pnl_sol = daily_pnl.pnl_sol + EXCLUDED.pnl_sol,
                    ending_balance_sol = COALESCE(EXCLUDED.ending_balance_sol, daily_pnl.ending_balance_sol),
                    updated_at = NOW()
                """,
                date_value,
                1 if won else 0,
                0 if won else 1,
                delta_pnl_sol,
                starting_balance,
                ending_balance,
            )

    async def get_daily_pnl(self, days: int = 30) -> list[dict[str, Any]]:
        """Last N days untuk PnL Calendar."""
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM daily_pnl
                WHERE date >= CURRENT_DATE - $1::int
                ORDER BY date DESC
                """,
                days,
            )
            return [dict(r) for r in rows]


# Singleton
_db: Database | None = None


async def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database()
        await _db.connect()
    return _db
