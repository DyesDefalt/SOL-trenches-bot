"""
Main Orchestrator — wire semua komponen + run forever.

Flow:
1. Init: load config, connect DB + Redis, init clients
2. Load smart wallet registry
3. Start background tasks:
   a. Position manager monitor (poll harga tiap 10s)
   b. Smart money tracker (Helius WS subscribe)
   c. Circuit breaker watchdog
   d. Telegram bot polling
   e. Smart wallet refresher (tiap 6 jam)
4. Main signal loop: scanner → enrich → score → execute (tiap 30s)

Run via:
    python -m src.main

Or via systemd:
    sudo systemctl start solana-bot
"""

from __future__ import annotations

import asyncio
import signal
from datetime import date
from typing import Any

from src.clients.geckoterminal import GeckoTerminalClient
from src.clients.gmgn import GMGNClient
from src.clients.gmgn_swap_client import GMGNSwapClient
from src.clients.helius import HeliusRPCClient, HeliusWSClient
from src.clients.helius_sender import HeliusSenderClient
from src.clients.jupiter import JupiterClient
from src.config import settings
from src.core.circuit_breaker import CircuitBreaker
from src.core.execution import ExecutionLayer
from src.core.position import OpenPosition, PositionManager
from src.core.scanner import TokenScanner
from src.core.scoring import ScoringEngine
from src.core.signal import SignalEngine
from src.core.smart_wallet_registry import SmartWalletRegistry
from src.core.tracker import SmartMoneyTracker
from src.infra.cache import cache
from src.infra.db import Database
from src.infra.health import HealthServer
from src.infra.logger import get_logger
from src.infra.telegram import TelegramBot
from src.infra.wallet import WalletManager

# Phase 7: Multi-Source Intelligence Layer
from src.intel.birdeye_client import BirdeyeClient
from src.intel.cluster_detector import ClusterDetector
from src.intel.dexscreener_client import DexscreenerClient
from src.intel.nansen_client import NansenClient
from src.intel.pumpfun_client import PumpfunClient
from src.intel.pumpfun_tracker import PumpfunTracker
from src.intel.rugcheck_client import RugcheckClient
from src.intel.smart_money import SmartMoneyAggregator
from src.intel.token_verifier import TokenVerifier

# Phase 9: Extended Intelligence (macro + news + cross-ref)
from src.clients.alphavantage_client import AlphaVantageClient
from src.clients.coingecko_client import CoinGeckoClient
from src.clients.cryptopanic_client import CryptoPanicClient
from src.clients.cryptoquant_client import CryptoQuantClient
from src.clients.messari_client import MessariClient
from src.intel.crossref_validator import CrossRefValidator
from src.intel.macro_regime import MacroRegimeDetector
from src.intel.news_aggregator import NewsAggregator

log = get_logger(__name__)


class Bot:
    """Main bot — wire semua komponen + lifecycle."""

    def __init__(self) -> None:
        # Clients
        self.gecko: GeckoTerminalClient | None = None
        self.gmgn: GMGNClient | None = None
        self.rpc: HeliusRPCClient | None = None
        self.ws: HeliusWSClient | None = None
        self.sender: HeliusSenderClient | None = None
        self.jupiter: JupiterClient | None = None

        # Phase 7g: GMGN Swap
        self.gmgn_swap: GMGNSwapClient | None = None

        # Phase 8: Health server
        self._health_server: HealthServer | None = None

        # Phase 7: Multi-Source Intelligence
        self.nansen: NansenClient | None = None
        self.birdeye: BirdeyeClient | None = None
        self.rugcheck: RugcheckClient | None = None
        self.dexscreener: DexscreenerClient | None = None
        self.pumpfun: PumpfunClient | None = None
        self.smart_money_aggregator: SmartMoneyAggregator | None = None
        self.cluster_detector: ClusterDetector | None = None
        self.token_verifier: TokenVerifier | None = None
        self.pumpfun_tracker: PumpfunTracker | None = None

        # Phase 9: Extended Intelligence (macro + news + cross-ref)
        self.cryptoquant: CryptoQuantClient | None = None
        self.alphavantage: AlphaVantageClient | None = None
        self.cryptopanic: CryptoPanicClient | None = None
        self.messari: MessariClient | None = None
        self.coingecko: CoinGeckoClient | None = None
        self.macro_detector: MacroRegimeDetector | None = None
        self.news_aggregator: NewsAggregator | None = None
        self.crossref_validator: CrossRefValidator | None = None

        # Infra
        self.db: Database | None = None
        self.wallet: WalletManager | None = None

        # Core
        self.registry: SmartWalletRegistry | None = None
        self.scanner: TokenScanner | None = None
        self.scoring: ScoringEngine | None = None
        self.tracker: SmartMoneyTracker | None = None
        self.signal: SignalEngine | None = None
        self.execution: ExecutionLayer | None = None
        self.position_manager: PositionManager | None = None
        self.cb: CircuitBreaker | None = None
        self.telegram: TelegramBot | None = None

        # Background tasks
        self._tasks: list[asyncio.Task] = []
        self._shutdown_event = asyncio.Event()

    async def setup(self) -> None:
        """Initialize semua komponen."""
        log.info("bot_setup_start", dry_run=settings.dry_run, env=settings.env)

        # Verify config
        missing = settings.assert_production_ready()
        if missing:
            log.error("config_missing", keys=missing)
            raise ValueError(f"Missing required env vars: {missing}")

        # Cache
        await cache.connect()

        # DB
        self.db = Database()
        await self.db.connect()

        # Wallet
        self.wallet = WalletManager()
        self.wallet.load()

        # Clients
        self.gecko = GeckoTerminalClient()
        self.gmgn = GMGNClient()
        self.rpc = HeliusRPCClient()
        self.ws = HeliusWSClient()
        self.sender = HeliusSenderClient()
        self.jupiter = JupiterClient()

        # Phase 7g: GMGN swap client (always init; only used when execution_provider="gmgn")
        self.gmgn_swap = GMGNSwapClient(wallet_address=self.wallet.address)

        # Phase 7: Multi-Source Intelligence clients
        # Nansen only initialized if API key available (graceful degrade)
        if settings.nansen_api_key:
            self.nansen = NansenClient()
        else:
            log.warning("nansen_disabled", note="NANSEN_API_KEY not set, smart money trend signals disabled")

        self.birdeye = BirdeyeClient()
        self.rugcheck = RugcheckClient()
        self.dexscreener = DexscreenerClient()
        self.pumpfun = PumpfunClient()

        # Phase 9: Extended Intelligence clients (all optional — graceful degrade)
        if settings.cryptoquant_api_key:
            self.cryptoquant = CryptoQuantClient()
        else:
            log.info("cryptoquant_disabled", note="CRYPTOQUANT_API_KEY not set, macro on-chain signals disabled")

        if settings.alphavantage_api_key:
            self.alphavantage = AlphaVantageClient()
        else:
            log.info("alphavantage_disabled", note="ALPHAVANTAGE_API_KEY not set, TradFi macro disabled")

        if settings.cryptopanic_api_key:
            self.cryptopanic = CryptoPanicClient()
        else:
            log.info("cryptopanic_disabled", note="CRYPTOPANIC_API_KEY not set, news sentiment disabled")

        if settings.messari_api_key:
            self.messari = MessariClient()
        else:
            log.info("messari_disabled", note="MESSARI_API_KEY not set, fundamentals cross-ref limited")

        # CoinGecko works on Demo tier with free key (highly recommended)
        if settings.coingecko_api_key:
            self.coingecko = CoinGeckoClient()
        else:
            log.info("coingecko_disabled", note="COINGECKO_API_KEY not set, cross-ref validation limited")

        # Smart wallet registry
        self.registry = SmartWalletRegistry()
        await self.registry.load()
        active_count = sum(1 for w in self.registry.get_active_wallets())
        log.info("registry_loaded", active_wallets=active_count)
        if active_count == 0:
            log.warning(
                "registry_empty",
                note="Run `make bootstrap-wallets` dulu, atau tambah manual via Telegram /addwallet",
            )

        # Phase 7: Enrich registry with Nansen labels (one-time per startup)
        if self.nansen:
            try:
                tier_counts = await self.registry.enrich_from_nansen(self.nansen)
                log.info("registry_nansen_enriched", **tier_counts)
            except Exception as e:
                log.warning("nansen_enrich_failed", error=str(e))

        # Scanner + scoring
        self.scoring = ScoringEngine()
        self.scanner = TokenScanner(gecko=self.gecko, gmgn=self.gmgn)

        # Phase 7: Build intel aggregators (only if dependencies available)
        if self.nansen:
            self.smart_money_aggregator = SmartMoneyAggregator(
                nansen_client=self.nansen,
                gmgn_client=self.gmgn,
                registry=self.registry,
            )

        self.cluster_detector = ClusterDetector(
            gmgn_client=self.gmgn,
            nansen_client=self.nansen,  # optional, can be None
        )

        # 5-source token verifier (degrades if Nansen unavailable)
        if self.nansen:
            self.token_verifier = TokenVerifier(
                gmgn_client=self.gmgn,
                nansen_client=self.nansen,
                rugcheck_client=self.rugcheck,
                dexscreener_client=self.dexscreener,
                birdeye_client=self.birdeye,
            )

        self.pumpfun_tracker = PumpfunTracker(pumpfun_client=self.pumpfun)

        # Phase 9: Build extended intelligence aggregators (all optional)
        if settings.macro_regime_enabled and (self.cryptoquant or self.alphavantage):
            self.macro_detector = MacroRegimeDetector(
                cryptoquant=self.cryptoquant,
                alphavantage=self.alphavantage,
            )
        else:
            log.info("macro_detector_disabled", note="No CryptoQuant or Alpha Vantage key — macro context off")

        if settings.news_narrative_enabled and (self.cryptopanic or self.messari):
            self.news_aggregator = NewsAggregator(
                cryptopanic=self.cryptopanic,
                messari=self.messari,
            )
        else:
            log.info("news_aggregator_disabled", note="No CryptoPanic or Messari key — narrative layer off")

        if settings.crossref_validation_enabled and (self.coingecko or self.messari):
            self.crossref_validator = CrossRefValidator(
                coingecko=self.coingecko,
                messari=self.messari,
            )
        else:
            log.info("crossref_validator_disabled", note="No CoinGecko or Messari key — cross-ref off")

        # Signal engine with intel layer
        self.signal = SignalEngine(
            scanner=self.scanner,
            gmgn=self.gmgn,
            gecko=self.gecko,
            registry=self.registry,
            scoring=self.scoring,
            smart_money_aggregator=self.smart_money_aggregator,
            cluster_detector=self.cluster_detector,
            token_verifier=self.token_verifier,
            pumpfun_tracker=self.pumpfun_tracker,
            # Phase 9: extended intelligence
            macro_detector=self.macro_detector,
            news_aggregator=self.news_aggregator,
            crossref_validator=self.crossref_validator,
        )

        # Execution + position manager
        self.execution = ExecutionLayer(
            wallet=self.wallet,
            jupiter=self.jupiter,
            sender=self.sender,
            rpc=self.rpc,
            gmgn_swap=self.gmgn_swap,
        )

        # Get initial balance untuk circuit breaker
        balance_lamports = await self.rpc.get_balance(self.wallet.address)
        balance_sol = balance_lamports / 1_000_000_000
        log.info("wallet_balance", sol=balance_sol)

        self.cb = CircuitBreaker(db=self.db, telegram=None)
        await self.cb.initialize(starting_balance_sol=balance_sol)

        self.position_manager = PositionManager(
            db=self.db,
            execution=self.execution,
            gecko=self.gecko,
            cb=self.cb,
            telegram=None,
        )
        await self.position_manager.load_open_positions()

        # Tracker (background WS subscriber)
        self.tracker = SmartMoneyTracker(
            ws=self.ws,
            registry=self.registry,
            cache=cache,
            max_wallets=100,
        )

        # Telegram (paling terakhir, perlu reference komponen lain)
        self.telegram = TelegramBot(
            registry=self.registry,
            cb=self.cb,
            position_manager=self.position_manager,
            db=self.db,
        )
        # Wire balik telegram ke cb dan position_manager untuk alerts
        self.cb.telegram = self.telegram
        self.position_manager.telegram = self.telegram

        # Phase 8: Health server
        self._health_server = HealthServer(port=settings.health_port, bot_ref=self)
        await self._health_server.start()
        self._health_server.mark_ready()

        log.info("bot_setup_complete")

    async def _signal_cycle(self) -> None:
        """One pass: scan + score + execute kalau action=BUY."""
        if not self.signal or not self.cb or not self.execution or not self.db or not self.position_manager:
            return

        if not self.cb.can_open_position():
            log.debug("signal_cycle_skip_paused", reason=self.cb.state.pause_reason)
            return

        if self.position_manager.open_count >= settings.max_concurrent_positions:
            log.debug("signal_cycle_skip_max_positions", count=self.position_manager.open_count)
            return

        try:
            results = await self.signal.evaluate_cycle(max_candidates=20)
        except Exception as e:
            log.error("signal_cycle_failed", error=str(e))
            return

        # Process actionable: BUY priority over ALERT
        for result in results:
            if not self.cb.can_open_position():
                break
            if self.position_manager.open_count >= settings.max_concurrent_positions:
                break

            if result.action == "BUY":
                await self._execute_buy(result)
            elif result.action == "ALERT" and self.telegram:
                await self.telegram.send_alert(
                    f"⚡ <b>SIGNAL</b> {result.token.symbol or result.token.address[:8]}\n"
                    f"Score: {result.score:.0f} (ALERT, manual review)\n"
                    f"SM count: {result.token.smart_money_count}\n"
                    f"MCAP: ${result.token.mcap_usd:,.0f}\n"
                    f"<code>{result.token.address}</code>"
                )

            # Persist signal ke DB (audit trail)
            try:
                await self.db.insert_signal(
                    token_address=result.token.address,
                    token_symbol=result.token.symbol,
                    score=result.score,
                    action=result.action,
                    reject_reasons=result.reject_reasons,
                    breakdown=result.to_dict()["breakdown"],
                    context=result.to_dict()["context"],
                    smart_money_count=result.token.smart_money_count,
                    smart_money_buyers=result.token.smart_money_buyers,
                )
            except Exception as e:
                log.warning("signal_persist_failed", error=str(e))

    async def _execute_buy(self, result) -> None:  # type: ignore[no-untyped-def]
        """Open new position based on score result."""
        if not self.execution or not self.scoring or not self.position_manager or not self.db:
            return

        token = result.token

        # Phase 9: Macro regime throttles position size (or zero if extreme risk-off)
        macro_mult = (
            token.macro_position_multiplier
            if settings.macro_regime_position_throttle_enabled
            else 1.0
        )
        sol_amount = self.scoring.position_size_sol(result.score, macro_multiplier=macro_mult)
        if sol_amount <= 0:
            log.info(
                "buy_skipped_macro_throttle",
                token=token.symbol or token.address[:8],
                score=result.score,
                macro_regime=token.macro_regime_level,
                macro_mult=macro_mult,
            )
            return

        log.info(
            "executing_buy",
            token=token.symbol or token.address[:8],
            score=result.score,
            sol_amount=sol_amount,
            sm_count=token.smart_money_count,
            macro_regime=token.macro_regime_level,
            macro_mult=macro_mult,
            narrative_match=token.narrative_match,
            crossref_listed=token.is_listed_on_coingecko,
        )

        trade = await self.execution.buy_token(
            token_address=token.address,
            sol_amount=sol_amount,
            slippage_bps=settings.slippage_bps,
            priority_fee_microlamports=settings.priority_fee_microlamports,
        )

        if not trade.success:
            log.warning("buy_failed", token=token.address[:8], error=trade.error)
            return

        # Persist position
        pos_id = await self.db.insert_position(
            token_address=token.address,
            token_symbol=token.symbol,
            token_name=token.name,
            entry_price_usd=token.price_usd,
            entry_amount_sol=sol_amount,
            entry_amount_token=trade.out_amount,
            entry_signature=trade.signature,
            entry_score=result.score,
            entry_smart_money_count=token.smart_money_count,
            dry_run=trade.dry_run,
        )

        from datetime import datetime, timezone

        pos = OpenPosition(
            db_id=pos_id,
            token_address=token.address,
            token_symbol=token.symbol,
            entry_price_usd=token.price_usd,
            entry_amount_sol=sol_amount,
            entry_amount_token=float(trade.out_amount),
            entry_timestamp=datetime.now(timezone.utc),
            peak_price_usd=token.price_usd,
            amount_remaining_token=float(trade.out_amount),
        )
        await self.position_manager.add_position(pos)

        if self.telegram:
            mode = "🧪 DRY" if trade.dry_run else "💵 LIVE"
            await self.telegram.send_alert(
                f"{mode} <b>BUY</b> {token.symbol or token.address[:8]}\n"
                f"Score: {result.score:.0f}\n"
                f"SM: {token.smart_money_count} wallets\n"
                f"Size: {sol_amount} SOL\n"
                f"Price impact: {trade.price_impact_pct:.1f}%\n"
                f"<code>{token.address}</code>"
            )

    async def _signal_loop(self, interval_seconds: int = 30) -> None:
        """Main signal scanning loop."""
        log.info("signal_loop_started", interval=interval_seconds)
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=interval_seconds)
                break  # shutdown signaled
            except asyncio.TimeoutError:
                pass

            try:
                await self._signal_cycle()
            except Exception as e:
                log.error("signal_loop_iter_failed", error=str(e))

    async def _registry_refresh_loop(self, interval_seconds: int = 6 * 3600) -> None:
        """Refresh smart wallet registry tiap 6 jam."""
        log.info("registry_refresh_loop_started", interval_hours=interval_seconds / 3600)
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=interval_seconds)
                break
            except asyncio.TimeoutError:
                pass

            if not self.registry or not self.gmgn:
                continue

            try:
                async with self.gmgn as g:
                    result = await self.registry.refresh(g)
                log.info("registry_refresh_done", **result)
                if self.telegram:
                    await self.telegram.send_alert(
                        f"🔄 <b>Smart wallet refreshed</b>\n"
                        f"A: {result.get('A', 0)}, B: {result.get('B', 0)}, C: {result.get('C', 0)}"
                    )
            except Exception as e:
                log.error("registry_refresh_failed", error=str(e))

    async def run(self) -> None:
        """Main run loop. Block sampai shutdown signal."""
        await self.setup()

        # Send startup alert
        if self.telegram:
            mode = "🧪 DRY_RUN" if settings.dry_run else "💵 LIVE"
            await self.telegram.send_alert(
                f"🚀 <b>Bot started</b> ({mode})\n"
                f"Wallet: <code>{self.wallet.address[:8]}...{self.wallet.address[-6:]}</code>\n"
                f"Balance: {self.cb.state.current_balance_sol:.4f} SOL"
            )

        # Start background tasks
        self._tasks = [
            asyncio.create_task(self.position_manager.run_monitor_loop(interval_seconds=10)),
            asyncio.create_task(self.cb.run_watchdog(interval_seconds=60)),
            asyncio.create_task(self._signal_loop(interval_seconds=30)),
            asyncio.create_task(self._registry_refresh_loop(interval_seconds=6 * 3600)),
        ]

        # Tracker (kalau registry punya wallet)
        if self.tracker:
            top_count = len(self.registry.get_top_tier_wallets()) if self.registry else 0
            if top_count > 0:
                self._tasks.append(asyncio.create_task(self.tracker.run()))
            else:
                log.warning("tracker_skipped_no_wallets")

        # Telegram
        if self.telegram:
            await self.telegram.start_polling()

        log.info("bot_running")

        # Wait for shutdown
        await self._shutdown_event.wait()
        await self._shutdown()

    async def _shutdown(self, reason: str = "signal") -> None:
        log.info("bot_shutdown_start", reason=reason)

        # Notify Telegram first (best-effort, short timeout)
        if self.telegram:
            try:
                await asyncio.wait_for(
                    self.telegram.send_alert("🛑 Bot shutting down..."),
                    timeout=5.0,
                )
                await self.telegram.stop()
            except Exception:
                pass

        # Cancel background tasks; give each up to 10s to finish cleanly
        for task in self._tasks:
            task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._tasks, return_exceptions=True),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            log.warning("shutdown_tasks_timeout", note="Some background tasks did not finish in 10s")

        # Health server (if started)
        health = getattr(self, "_health_server", None)
        if health is not None:
            try:
                health.mark_not_ready()
                await asyncio.wait_for(health.stop(), timeout=5.0)
            except Exception as e:
                log.warning("health_server_stop_error", error=str(e))

        # Close clients in reverse dependency order
        # (intel layer closed first, then transport clients, then infra)
        ordered_clients = [
            # Phase 9 extended intel (depend on base clients) — close first
            self.coingecko,
            self.messari,
            self.cryptopanic,
            self.alphavantage,
            self.cryptoquant,
            # Phase 7 intel (depend on base clients)
            self.pumpfun,
            self.dexscreener,
            self.rugcheck,
            self.birdeye,
            self.nansen,
            # Phase 7g GMGN swap (no HTTP client, just subprocess — close is no-op)
            # Base API clients
            self.pumpfun_tracker,
            self.jupiter,
            self.sender,
            self.ws,
            self.rpc,
            self.gmgn,
            self.gecko,
        ]
        for client in ordered_clients:
            if client is None:
                continue
            close_fn = getattr(client, "close", None)
            if close_fn is None:
                continue
            try:
                await asyncio.wait_for(close_fn(), timeout=5.0)
            except Exception:
                pass

        # Infra (DB + cache last)
        if self.db:
            try:
                await asyncio.wait_for(self.db.close(), timeout=5.0)
            except Exception:
                pass
        try:
            await asyncio.wait_for(cache.close(), timeout=5.0)
        except Exception:
            pass

        log.info("bot_shutdown_complete", reason=reason)

    def trigger_shutdown(self) -> None:
        self._shutdown_event.set()


async def main() -> None:
    bot = Bot()

    loop = asyncio.get_running_loop()

    def signal_handler() -> None:
        log.info("signal_received_shutdown")
        bot.trigger_shutdown()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            # Windows tidak support
            pass

    try:
        await bot.run()
    except Exception as e:
        log.exception("bot_fatal_error", error=str(e))
        raise


if __name__ == "__main__":
    asyncio.run(main())
