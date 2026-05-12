"""
Backtest Replay Engine — simulate bot trading di data historis.

Strategi:
1. Untuk tiap token di dataset, walk through OHLCV candle-by-candle
2. Pada timestamp T, simulate scoring engine:
   - Compute features (price, volume_5m, volume_15m, volume_increasing)
   - Sample smart money count (untuk MVP: random 0-3 weighted by token momentum)
   - Run ScoringEngine
3. Kalau action=BUY, open simulated position
4. Track exit conditions (TP staircase, trailing, SL, time)
5. Aggregate semua trade untuk hitung metrics

LIMITASI HONEST:
- Smart money count di backtest tidak akurat tanpa archive Solana state
- Simulate via heuristic: tokens dengan momentum (volume increasing + price up
  early) get higher SM count, vice versa. Bukan sempurna tapi memberikan
  proxy yang reasonable.
- Untuk akurasi tinggi, perlu raw on-chain data archive (mahal, beyond MVP).

Output: BacktestResult dengan list trades + aggregate metrics.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from src.config import settings
from src.core.scoring import ScoreResult, ScoringEngine, TokenData
from src.infra.logger import get_logger

if TYPE_CHECKING:
    pass

log = get_logger(__name__)


@dataclass
class SimulatedTrade:
    """Single trade hasil replay."""

    token_address: str
    symbol: str
    entry_timestamp: datetime
    entry_price_usd: float
    entry_amount_sol: float
    entry_score: float

    exit_timestamp: datetime | None = None
    exit_price_usd: float = 0.0
    exit_amount_sol: float = 0.0
    exit_reason: str = ""

    pnl_sol: float = 0.0
    pnl_pct: float = 0.0
    won: bool = False
    holding_minutes: float = 0.0

    # Slippage + fee accounting (subtracted dari pnl)
    slippage_cost_sol: float = 0.0
    fee_cost_sol: float = 0.0


@dataclass
class BacktestResult:
    """Aggregate metrics + per-trade list."""

    trades: list[SimulatedTrade] = field(default_factory=list)
    initial_capital_sol: float = 0.36

    @property
    def trade_count(self) -> int:
        return len(self.trades)

    @property
    def win_count(self) -> int:
        return sum(1 for t in self.trades if t.won)

    @property
    def win_rate(self) -> float:
        return self.win_count / self.trade_count if self.trade_count > 0 else 0

    @property
    def total_pnl_sol(self) -> float:
        return sum(t.pnl_sol for t in self.trades)

    @property
    def total_return_pct(self) -> float:
        if self.initial_capital_sol == 0:
            return 0
        return (self.total_pnl_sol / self.initial_capital_sol) * 100

    @property
    def gross_profit(self) -> float:
        return sum(t.pnl_sol for t in self.trades if t.pnl_sol > 0)

    @property
    def gross_loss(self) -> float:
        return abs(sum(t.pnl_sol for t in self.trades if t.pnl_sol < 0))

    @property
    def profit_factor(self) -> float:
        if self.gross_loss == 0:
            return float("inf") if self.gross_profit > 0 else 0
        return self.gross_profit / self.gross_loss

    @property
    def max_drawdown_pct(self) -> float:
        """Max drawdown dari peak equity selama backtest."""
        if not self.trades:
            return 0
        equity = self.initial_capital_sol
        peak = equity
        max_dd = 0.0
        for t in sorted(self.trades, key=lambda x: x.exit_timestamp or x.entry_timestamp):
            equity += t.pnl_sol
            if equity > peak:
                peak = equity
            if peak > 0:
                dd = (equity - peak) / peak * 100
                if dd < max_dd:
                    max_dd = dd
        return abs(max_dd)

    @property
    def avg_holding_minutes(self) -> float:
        if not self.trades:
            return 0
        return sum(t.holding_minutes for t in self.trades) / len(self.trades)

    def to_summary_dict(self) -> dict:
        return {
            "initial_capital_sol": self.initial_capital_sol,
            "trade_count": self.trade_count,
            "win_count": self.win_count,
            "win_rate": round(self.win_rate, 4),
            "total_pnl_sol": round(self.total_pnl_sol, 6),
            "total_return_pct": round(self.total_return_pct, 2),
            "gross_profit_sol": round(self.gross_profit, 6),
            "gross_loss_sol": round(self.gross_loss, 6),
            "profit_factor": round(self.profit_factor, 2) if self.profit_factor != float("inf") else "inf",
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "avg_holding_minutes": round(self.avg_holding_minutes, 1),
        }


class ReplayEngine:
    """Replay historical data → simulate trading → output metrics."""

    def __init__(
        self,
        scoring: ScoringEngine | None = None,
        initial_capital_sol: float = 0.36,
        slippage_pct: float = 0.05,  # 5% — realistic untuk memecoin
        fee_per_trade_sol: float = 0.005,  # priority fee + tip
        max_concurrent_positions: int = 2,
        rng_seed: int = 42,
    ) -> None:
        self.scoring = scoring or ScoringEngine()
        self.initial_capital_sol = initial_capital_sol
        self.slippage_pct = slippage_pct
        self.fee_per_trade_sol = fee_per_trade_sol
        self.max_concurrent = max_concurrent_positions
        self.rng = random.Random(rng_seed)

    def run(self, historical_dataset: list[dict]) -> BacktestResult:
        """
        Replay seluruh dataset, return aggregate result.

        historical_dataset format dari HistoricalDataFetcher.
        """
        result = BacktestResult(initial_capital_sol=self.initial_capital_sol)

        for token_data in historical_dataset:
            try:
                trade = self._replay_single_token(token_data)
                if trade:
                    result.trades.append(trade)
            except Exception as e:
                log.warning(
                    "replay_token_failed",
                    token=token_data.get("address", "?")[:8],
                    error=str(e),
                )

        log.info("replay_done", **result.to_summary_dict())
        return result

    def _replay_single_token(self, token_data: dict) -> SimulatedTrade | None:
        """
        Replay satu token. Logic:
        1. Walk through candles dari awal
        2. Saat kondisi entry trigger (score >= 75 di candle current state) → buy
        3. Track candle-by-candle untuk evaluate exit
        4. Stop saat exit trigger atau end of data
        """
        ohlcv = token_data.get("ohlcv", [])
        if len(ohlcv) < 30:  # need minimal history untuk meaningful replay
            return None

        symbol = token_data.get("symbol", "?")
        address = token_data.get("address", "")
        metadata = token_data.get("metadata", {})

        # Iterate candles, looking for entry signal
        for i in range(20, len(ohlcv) - 5):  # need lookback + lookforward
            entry_candle = ohlcv[i]
            entry_ts, _, _, _, entry_close, entry_volume = entry_candle

            # Build TokenData snapshot di timestamp ini
            token_snapshot = self._build_snapshot(
                token_address=address,
                symbol=symbol,
                metadata=metadata,
                ohlcv=ohlcv,
                index=i,
            )

            # Run scoring
            score_result = self.scoring.score(token_snapshot)

            if score_result.action != "BUY":
                continue

            # ENTER position
            entry_size_sol = self.scoring.position_size_sol(score_result.score)
            if entry_size_sol == 0:
                continue

            # Apply slippage + fee
            effective_entry_price = entry_close * (1 + self.slippage_pct)
            entry_token_amount = entry_size_sol / effective_entry_price * (entry_close)  # rough

            # Walk forward sampai exit trigger
            trade = self._simulate_position_lifecycle(
                token_address=address,
                symbol=symbol,
                ohlcv=ohlcv,
                entry_index=i,
                entry_price_usd=entry_close,  # naive: ignore slippage in exit price comparison
                entry_amount_sol=entry_size_sol,
                entry_score=score_result.score,
            )
            return trade

        return None

    def _build_snapshot(
        self,
        token_address: str,
        symbol: str,
        metadata: dict,
        ohlcv: list[list],
        index: int,
    ) -> TokenData:
        """Construct TokenData untuk timestamp = index."""
        current = ohlcv[index]
        ts, _, _, _, close, _ = current

        # Volume 5m = current candle volume (kalau resolution=minute, aggregate=5)
        vol_5m = float(current[5])

        # Volume 15m = 3 candles (rolling)
        vol_15m = sum(float(ohlcv[i][5]) for i in range(max(0, index - 2), index + 1))

        # Volume increasing: vol_5m > avg(prev 3 candles)
        prev_avg = sum(float(ohlcv[i][5]) for i in range(max(0, index - 3), index)) / max(1, min(index, 3))
        vol_increasing = vol_5m > prev_avg * 1.5

        # Price ATH: max close di window sebelumnya
        prev_window = ohlcv[max(0, index - 100) : index]
        ath = max((float(c[2]) for c in prev_window), default=close)  # high

        # Smart money count: simulate dengan heuristic
        # Lebih banyak SM kalau volume increasing + price below recent ATH
        sm_count = 0
        if vol_increasing and close < ath * 0.7:
            sm_count = self.rng.choices([0, 1, 2, 3, 4], weights=[20, 30, 30, 15, 5])[0]
        elif vol_increasing:
            sm_count = self.rng.choices([0, 1, 2, 3], weights=[40, 35, 20, 5])[0]
        else:
            sm_count = self.rng.choices([0, 1, 2], weights=[70, 25, 5])[0]

        # MCAP — approximation (price × supply)
        supply = float(metadata.get("total_supply", 1_000_000_000) or 1_000_000_000)
        decimals = int(metadata.get("decimals", 9) or 9)
        normalized_supply = supply / (10**decimals)
        mcap = close * normalized_supply

        # Liquidity — assume reasonable based on volume (heuristic)
        liquidity = max(vol_15m * 2, 8000)

        return TokenData(
            address=token_address,
            symbol=symbol,
            mcap_usd=mcap,
            liquidity_usd=liquidity,
            price_usd=close,
            price_ath=ath,
            volume_5m_usd=vol_5m,
            volume_15m_usd=vol_15m,
            volume_increasing=vol_increasing,
            smart_money_count=sm_count,
            kol_count=0,
            is_honeypot=False,
            lp_burned=True,  # assume well-known tokens have LP burned
            is_renounced=True,
            gmgn_security_score=80,  # assume passing
            dev_holding_pct=5,
            bundle_supply_pct=5,
        )

    def _simulate_position_lifecycle(
        self,
        token_address: str,
        symbol: str,
        ohlcv: list[list],
        entry_index: int,
        entry_price_usd: float,
        entry_amount_sol: float,
        entry_score: float,
    ) -> SimulatedTrade:
        """Walk forward dari entry, evaluate exit kondisi tiap candle."""
        entry_ts = ohlcv[entry_index][0]
        peak_price = entry_price_usd
        amount_remaining_pct = 1.0  # 100%
        partial_pnl_sol = 0.0
        tp1_done = False
        tp2_done = False
        tp3_done = False

        trade = SimulatedTrade(
            token_address=token_address,
            symbol=symbol,
            entry_timestamp=datetime.fromtimestamp(entry_ts, tz=timezone.utc),
            entry_price_usd=entry_price_usd,
            entry_amount_sol=entry_amount_sol,
            entry_score=entry_score,
            slippage_cost_sol=entry_amount_sol * self.slippage_pct,
            fee_cost_sol=self.fee_per_trade_sol * 2,  # buy + sell fees
        )

        for j in range(entry_index + 1, len(ohlcv)):
            ts, _, high, low, close, _ = ohlcv[j]
            elapsed_min = (ts - entry_ts) / 60

            if close > peak_price:
                peak_price = close

            gain_pct = ((close - entry_price_usd) / entry_price_usd) * 100

            # Hard SL (use low untuk worst-case)
            low_gain_pct = ((low - entry_price_usd) / entry_price_usd) * 100
            if low_gain_pct <= settings.hard_sl_pct:
                # Full exit at SL
                exit_price = entry_price_usd * (1 + settings.hard_sl_pct / 100)
                exit_sol = entry_amount_sol * amount_remaining_pct * (1 + settings.hard_sl_pct / 100)
                exit_sol *= 1 - self.slippage_pct  # exit slippage
                trade.exit_timestamp = datetime.fromtimestamp(ts, tz=timezone.utc)
                trade.exit_price_usd = exit_price
                trade.exit_amount_sol = exit_sol + partial_pnl_sol
                trade.exit_reason = "SL"
                break

            # Trailing stop (after TP3)
            drop_from_peak = ((close - peak_price) / peak_price) * 100
            if tp3_done and drop_from_peak <= -settings.trailing_stop_pct:
                exit_price = close
                exit_sol = entry_amount_sol * amount_remaining_pct * (close / entry_price_usd) * (1 - self.slippage_pct)
                trade.exit_timestamp = datetime.fromtimestamp(ts, tz=timezone.utc)
                trade.exit_price_usd = exit_price
                trade.exit_amount_sol = exit_sol + partial_pnl_sol
                trade.exit_reason = "TRAILING"
                break

            # TP staircase
            if not tp1_done and high >= entry_price_usd * (1 + settings.tp1_gain_pct / 100):
                sell_pct = settings.tp1_sell_pct / 100
                sell_amount_sol = entry_amount_sol * sell_pct * (1 + settings.tp1_gain_pct / 100) * (1 - self.slippage_pct)
                partial_pnl_sol += sell_amount_sol
                amount_remaining_pct -= sell_pct
                tp1_done = True
                continue

            if not tp2_done and high >= entry_price_usd * (1 + settings.tp2_gain_pct / 100):
                sell_pct = settings.tp2_sell_pct / 100
                sell_amount_sol = entry_amount_sol * sell_pct * (1 + settings.tp2_gain_pct / 100) * (1 - self.slippage_pct)
                partial_pnl_sol += sell_amount_sol
                amount_remaining_pct -= sell_pct
                tp2_done = True
                continue

            if not tp3_done and high >= entry_price_usd * (1 + settings.tp3_gain_pct / 100):
                sell_pct = settings.tp3_sell_pct / 100
                sell_amount_sol = entry_amount_sol * sell_pct * (1 + settings.tp3_gain_pct / 100) * (1 - self.slippage_pct)
                partial_pnl_sol += sell_amount_sol
                amount_remaining_pct -= sell_pct
                tp3_done = True
                continue

            # Time-based exit
            if elapsed_min >= settings.time_based_exit_minutes and gain_pct < 20 and not tp1_done:
                exit_price = close
                exit_sol = entry_amount_sol * amount_remaining_pct * (close / entry_price_usd) * (1 - self.slippage_pct)
                trade.exit_timestamp = datetime.fromtimestamp(ts, tz=timezone.utc)
                trade.exit_price_usd = exit_price
                trade.exit_amount_sol = exit_sol + partial_pnl_sol
                trade.exit_reason = "TIME_EXIT"
                break

        # End of data without exit — close at last price
        if not trade.exit_timestamp:
            last_ts, _, _, _, last_close, _ = ohlcv[-1]
            exit_sol = entry_amount_sol * amount_remaining_pct * (last_close / entry_price_usd) * (1 - self.slippage_pct)
            trade.exit_timestamp = datetime.fromtimestamp(last_ts, tz=timezone.utc)
            trade.exit_price_usd = last_close
            trade.exit_amount_sol = exit_sol + partial_pnl_sol
            trade.exit_reason = "END_OF_DATA"

        # Calculate PnL
        trade.pnl_sol = trade.exit_amount_sol - entry_amount_sol - trade.fee_cost_sol
        trade.pnl_pct = (trade.pnl_sol / entry_amount_sol) * 100 if entry_amount_sol > 0 else 0
        trade.won = trade.pnl_sol > 0
        if trade.exit_timestamp:
            trade.holding_minutes = (trade.exit_timestamp - trade.entry_timestamp).total_seconds() / 60

        return trade
