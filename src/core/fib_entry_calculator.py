"""
Phase 10.6: Fibonacci Entry Calculator.

Implements the Fibonacci retracement entry helper recommended by @badidoyo.
After a price runs up and pulls back, calculates Fibonacci levels from the
most recent swing high/low and recommends ENTER_NOW vs WAIT_FOR_DIP based
on current price relative to the 0.786 level (default target).

Strategy:
- Fetch recent OHLC candles (default: 5m, 100 bars)
- Detect most recent swing high and the swing low that preceded it
- Require minimum 50% move (swing_high / swing_low >= 1.5) for meaningful fib
- Target: 0.786 retracement (deep pullback, highest R:R per @badidoyo)
- ENTER_NOW  → current price is at or below the target fib level
- WAIT_FOR_DIP → price still above target, set a price alert
- OUT_OF_RANGE → price below the swing low (over-extended dump)
- NO_DATA     → insufficient OHLC history or no valid swing found
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.infra.logger import get_logger

if TYPE_CHECKING:
    from src.clients.geckoterminal import GeckoTerminalClient

log = get_logger(__name__)

# Canonical set of Fibonacci retracement ratios tracked by this module
_FIB_RATIOS: dict[str, float] = {
    "0.236": 0.236,
    "0.382": 0.382,
    "0.5":   0.500,
    "0.618": 0.618,
    "0.786": 0.786,
    "0.886": 0.886,
}

# Default ratio key used as the entry trigger (per @badidoyo recommendation)
_DEFAULT_TARGET_LEVEL = "0.786"

# Minimum ratio swing_high/swing_low to consider a swing meaningful
_MIN_SWING_RATIO = 1.5

# Sliding window half-width for local extrema detection
_DEFAULT_WINDOW = 5

# Recommendation constants
ENTER_NOW    = "ENTER_NOW"
WAIT_FOR_DIP = "WAIT_FOR_DIP"
OUT_OF_RANGE = "OUT_OF_RANGE"
NO_DATA      = "NO_DATA"

# Timeframe string → (gecko timeframe literal, aggregate integer)
_TIMEFRAME_MAP: dict[str, tuple[str, int]] = {
    "1m":  ("minute", 1),
    "5m":  ("minute", 5),
    "15m": ("minute", 15),
    "1h":  ("hour",   1),
    "4h":  ("hour",   4),
    "1d":  ("day",    1),
}


@dataclass
class FibAnalysis:
    """
    Full Fibonacci analysis result for one token.

    Attributes
    ----------
    swing_high_price     : Price at the detected swing high candle.
    swing_low_price      : Price at the detected swing low candle (precedes high).
    swing_high_at_ms     : Unix timestamp (ms) of the swing high candle.
    swing_low_at_ms      : Unix timestamp (ms) of the swing low candle.
    current_price        : Price at the time of analysis.
    fib_levels           : Dict mapping level key → absolute price.
    target_fib_level     : The fib key used as entry trigger (default "0.786").
    target_price         : Absolute price of the target fib level.
    distance_to_target_pct : (current - target) / target * 100. Negative = already below.
    is_above_target      : True if current_price > target_price.
    should_wait          : True when WAIT_FOR_DIP is the recommendation.
    reasoning            : Human-readable explanation string.
    """

    swing_high_price: float
    swing_low_price: float
    swing_high_at_ms: int
    swing_low_at_ms: int
    current_price: float
    fib_levels: dict[str, float] = field(default_factory=dict)
    target_fib_level: str = _DEFAULT_TARGET_LEVEL
    target_price: float = 0.0
    distance_to_target_pct: float = 0.0
    is_above_target: bool = True
    should_wait: bool = True
    reasoning: str = ""


class FibEntryCalculator:
    """
    Fibonacci retracement entry helper.

    Usage (with live gecko):
        calc = FibEntryCalculator(gecko=gecko_client)
        analysis = await calc.compute("So11111111111111111111111111111111111111112")

    Usage (unit tests, no gecko):
        calc = FibEntryCalculator(gecko=None)
        # use _find_swings / _compute_fib_levels directly with synthetic data
    """

    def __init__(self, gecko: "GeckoTerminalClient | None") -> None:
        self._gecko = gecko

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def compute(
        self,
        token_address: str,
        network: str = "solana",
        timeframe: str = "5m",
        lookback_periods: int = 100,
    ) -> FibAnalysis | None:
        """
        Fetch OHLC data, detect swing high/low, compute all fib levels.

        Parameters
        ----------
        token_address    : Solana (or other chain) token mint address.
        network          : GeckoTerminal network slug (default "solana").
        timeframe        : Candle timeframe string — "1m", "5m", "15m", "1h",
                           "4h", "1d".
        lookback_periods : Number of candles to request (max 1000).

        Returns
        -------
        FibAnalysis if a valid swing pair is found, None otherwise.
        """
        if self._gecko is None:
            log.warning("fib_no_gecko", token=token_address)
            return None

        tf_entry = _TIMEFRAME_MAP.get(timeframe)
        if tf_entry is None:
            log.warning("fib_unknown_timeframe", timeframe=timeframe)
            return None

        tf_literal, aggregate = tf_entry

        try:
            ohlc = await self._gecko.get_token_ohlcv(
                token_address,
                timeframe=tf_literal,    # type: ignore[arg-type]
                aggregate=aggregate,
                limit=lookback_periods,
                network=network,
            )
        except Exception as exc:
            log.warning("fib_gecko_error", token=token_address, error=str(exc))
            return None

        if not ohlc or len(ohlc) < (_DEFAULT_WINDOW * 2 + 1):
            log.debug("fib_insufficient_data", token=token_address, candles=len(ohlc) if ohlc else 0)
            return None

        result = self._find_swings(ohlc)
        if result is None:
            log.debug("fib_no_swing", token=token_address)
            return None

        swing_high, swing_low = result

        fib_levels = self._compute_fib_levels(swing_high["price"], swing_low["price"])
        target_price = fib_levels[_DEFAULT_TARGET_LEVEL]

        # Current price = close of the most recent candle
        latest = ohlc[-1]
        current_price = float(latest[4])  # [ts, open, high, low, close, vol]

        distance_to_target_pct = (current_price - target_price) / target_price * 100
        is_above_target = current_price > target_price

        if is_above_target:
            should_wait = True
            pct_away = abs(distance_to_target_pct)
            reasoning = (
                f"Price {current_price:.6g} is {pct_away:.1f}% above the "
                f"{_DEFAULT_TARGET_LEVEL} fib target ({target_price:.6g}). "
                f"Wait for pullback to enter. Swing: "
                f"{swing_low['price']:.6g} → {swing_high['price']:.6g}."
            )
        elif current_price < swing_low["price"]:
            should_wait = False
            reasoning = (
                f"Price {current_price:.6g} is below swing low "
                f"{swing_low['price']:.6g} — fib structure invalidated (out of range)."
            )
        else:
            should_wait = False
            pct_below = abs(distance_to_target_pct)
            reasoning = (
                f"Price {current_price:.6g} is {pct_below:.1f}% below the "
                f"{_DEFAULT_TARGET_LEVEL} fib level ({target_price:.6g}). "
                f"Entry zone reached. Swing: "
                f"{swing_low['price']:.6g} → {swing_high['price']:.6g}."
            )

        return FibAnalysis(
            swing_high_price=swing_high["price"],
            swing_low_price=swing_low["price"],
            swing_high_at_ms=swing_high["ts_ms"],
            swing_low_at_ms=swing_low["ts_ms"],
            current_price=current_price,
            fib_levels=fib_levels,
            target_fib_level=_DEFAULT_TARGET_LEVEL,
            target_price=target_price,
            distance_to_target_pct=distance_to_target_pct,
            is_above_target=is_above_target,
            should_wait=should_wait,
            reasoning=reasoning,
        )

    async def suggest_fib_entry(
        self,
        token_address: str,
        current_price: float,
        min_drop_pct: float = 5.0,
    ) -> tuple[str, float, str] | None:
        """
        High-level entry recommendation for a token at a given price.

        Runs compute() with default parameters and maps the result to a
        simple (recommendation, target_price, reasoning) tuple.

        Parameters
        ----------
        token_address : Token mint address.
        current_price : Externally supplied current price (overrides last candle
                        close from OHLC — useful when caller has a live price).
        min_drop_pct  : Minimum percentage drop from swing high required for the
                        analysis to be meaningful. If the current price is within
                        this distance of the swing high (i.e. the token hasn't
                        really pulled back yet), recommend WAIT_FOR_DIP.

        Returns
        -------
        (recommendation, target_price, reasoning) or None if no data.
        Recommendation values: ENTER_NOW, WAIT_FOR_DIP, OUT_OF_RANGE, NO_DATA.
        """
        analysis = await self.compute(token_address)
        if analysis is None:
            return (NO_DATA, 0.0, "Insufficient OHLC data to compute fib levels.")

        target_price = analysis.target_price
        swing_high   = analysis.swing_high_price
        swing_low    = analysis.swing_low_price

        # Out-of-range: price collapsed below the entire swing
        if current_price < swing_low:
            reasoning = (
                f"Price {current_price:.6g} is below swing low "
                f"{swing_low:.6g}. Fib structure invalid."
            )
            return (OUT_OF_RANGE, target_price, reasoning)

        # Has the price even dropped min_drop_pct from the swing high?
        drop_from_high_pct = (swing_high - current_price) / swing_high * 100
        if drop_from_high_pct < min_drop_pct:
            reasoning = (
                f"Price {current_price:.6g} is only {drop_from_high_pct:.1f}% "
                f"below swing high {swing_high:.6g} (need {min_drop_pct}%). "
                f"Wait for pullback."
            )
            return (WAIT_FOR_DIP, target_price, reasoning)

        if current_price <= target_price:
            reasoning = (
                f"Price {current_price:.6g} has reached the {_DEFAULT_TARGET_LEVEL} "
                f"fib level ({target_price:.6g}). Enter now per @badidoyo strategy."
            )
            return (ENTER_NOW, target_price, reasoning)

        pct_away = (current_price - target_price) / target_price * 100
        reasoning = (
            f"Price {current_price:.6g} is {pct_away:.1f}% above the "
            f"{_DEFAULT_TARGET_LEVEL} fib target ({target_price:.6g}). "
            f"Wait for dip to entry zone."
        )
        return (WAIT_FOR_DIP, target_price, reasoning)

    # ------------------------------------------------------------------
    # Core algorithms (pure functions — no I/O, easy to test)
    # ------------------------------------------------------------------

    def _find_swings(
        self,
        ohlc: list[list],
        window: int = _DEFAULT_WINDOW,
    ) -> tuple[dict, dict] | None:
        """
        Locate the most recent local swing high, then find the most recent
        local swing low that occurred *before* that high.

        Each candle element is [timestamp_unix_s, open, high, low, close, volume].
        Timestamps from GeckoTerminal are Unix seconds; we convert to ms for the
        output dataclass.

        Local high rule  : candle[i].high == max(high values in [i-window, i+window])
        Local low  rule  : candle[i].low  == min(low  values in [i-window, i+window])

        Minimum swing requirement:
            swing_high_price / swing_low_price >= _MIN_SWING_RATIO

        Returns
        -------
        (swing_high_dict, swing_low_dict) or None.
        Each dict: {"price": float, "ts_ms": int, "index": int}
        """
        n = len(ohlc)
        min_idx = window
        max_idx = n - window - 1  # inclusive

        if max_idx < min_idx:
            return None

        # ---- collect all local highs (scan right to left for most recent first)
        local_highs: list[dict] = []
        for i in range(max_idx, min_idx - 1, -1):
            hi_val = float(ohlc[i][2])  # index 2 = high
            window_highs = [float(ohlc[j][2]) for j in range(i - window, i + window + 1)]
            if hi_val == max(window_highs):
                local_highs.append({
                    "price":  hi_val,
                    "ts_ms":  int(ohlc[i][0]) * 1000,
                    "index":  i,
                })

        if not local_highs:
            return None

        # ---- for each local high (most recent first), find the best preceding low
        for swing_high in local_highs:
            high_idx = swing_high["index"]

            # Search for local lows strictly before swing_high
            best_low: dict | None = None
            for i in range(high_idx - 1, min_idx - 1, -1):
                lo_val = float(ohlc[i][3])  # index 3 = low
                window_lows = [float(ohlc[j][3]) for j in range(i - window, i + window + 1)]
                if lo_val == min(window_lows):
                    # Enforce minimum swing ratio
                    if swing_high["price"] > 0 and swing_high["price"] / lo_val >= _MIN_SWING_RATIO:
                        best_low = {
                            "price":  lo_val,
                            "ts_ms":  int(ohlc[i][0]) * 1000,
                            "index":  i,
                        }
                        break  # most recent qualifying low wins

            if best_low is not None:
                return (swing_high, best_low)

        # No valid swing pair found
        return None

    @staticmethod
    def _compute_fib_levels(
        swing_high: float,
        swing_low: float,
    ) -> dict[str, float]:
        """
        Compute Fibonacci retracement levels from a swing high/low pair.

        Formula: level = swing_high - (swing_high - swing_low) * ratio

        Example (swing_high=2.0, swing_low=1.0):
            range = 1.0
            0.236 → 2.0 - 0.236 = 1.764
            0.382 → 2.0 - 0.382 = 1.618
            0.5   → 2.0 - 0.500 = 1.500
            0.618 → 2.0 - 0.618 = 1.382
            0.786 → 2.0 - 0.786 = 1.214
            0.886 → 2.0 - 0.886 = 1.114
        """
        range_ = swing_high - swing_low
        return {
            "0.236": swing_high - range_ * 0.236,
            "0.382": swing_high - range_ * 0.382,
            "0.5":   swing_high - range_ * 0.500,
            "0.618": swing_high - range_ * 0.618,
            "0.786": swing_high - range_ * 0.786,
            "0.886": swing_high - range_ * 0.886,
        }
