"""
Tests for Phase 10.6: FibEntryCalculator.

Coverage (14 tests):
  1.  fib math exact — swing 2.0/1.0 → verify all 6 levels
  2.  fib math exact — swing 1.5/1.0 → verify all 6 levels
  3.  local extrema detection — single clear swing in synthetic OHLC
  4.  current price above 0.786 → should_wait=True, is_above_target=True
  5.  current price at 0.786 → ENTER_NOW (is_above_target=False)
  6.  current price below 0.786 → is_above_target=False, should_wait=False
  7.  insufficient swing ratio (< 1.5x) → _find_swings returns None → compute returns None
  8.  no OHLC data (empty list) → compute returns None
  9.  multiple local highs → most recent qualifying swing chosen
 10.  suggest_fib_entry: current below target → ENTER_NOW
 11.  suggest_fib_entry: current above target → WAIT_FOR_DIP
 12.  suggest_fib_entry: no data (gecko returns empty) → NO_DATA
 13.  suggest_fib_entry: price below swing low → OUT_OF_RANGE
 14.  suggest_fib_entry: price not dropped enough (< min_drop_pct) → WAIT_FOR_DIP
"""

from __future__ import annotations

import math
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.core.fib_entry_calculator import (
    ENTER_NOW,
    NO_DATA,
    OUT_OF_RANGE,
    WAIT_FOR_DIP,
    FibAnalysis,
    FibEntryCalculator,
)


# =============================================================================
# Helpers
# =============================================================================

def _make_candle(ts: int, open_: float, high: float, low: float, close: float) -> list:
    """Return a GeckoTerminal-format OHLC candle: [ts_unix_s, o, h, l, c, volume]."""
    return [ts, open_, high, low, close, 1000.0]


def _flat_candles(n: int, price: float = 1.0, start_ts: int = 1_700_000_000) -> list[list]:
    """N candles all at the same price (no swings)."""
    return [_make_candle(start_ts + i * 300, price, price, price, price) for i in range(n)]


def _build_synthetic_ohlc(
    base_ts: int = 1_700_000_000,
    interval: int = 300,
) -> list[list]:
    """
    Build a synthetic OHLC sequence with one clear swing detectable by
    the 5-candle sliding-window algorithm.

    Structure (window=5, so local extrema need 5 neighbours on each side):
      - Candles 0-4:   flat preamble at 1.05 (padding for window)
      - Candle 5:      local LOW at 1.0  (surrounded by higher candles)
      - Candles 6-10:  rising from 1.05 to 1.50
      - Candles 11-15: continue rising 1.55 to 1.75
      - Candle 16:     swing HIGH at 3.0
      - Candles 17-21: pullback 2.8 → 2.0  (current close ≈ 2.0)

    Total: 22 candles.
    Swing: low=1.0 (candle 5), high=3.0 (candle 16)  → ratio 3.0x ≥ 1.5
    0.786 level: 3.0 - (3.0 - 1.0) * 0.786 = 3.0 - 1.572 = 1.428

    Candle 5 is a local low: its low (1.0) is the minimum of the
    window [0..10], which is all 1.0 or 1.05, so 1.0 < 1.05 → it IS the min.
    Candle 16 is a local high: its high (3.0) is the max of window [11..21],
    where no other candle exceeds 1.75+ε.
    """
    candles: list[list] = []
    ts = base_ts

    # Candles 0-4: flat preamble (slightly above the local low)
    for _ in range(5):
        candles.append(_make_candle(ts, 1.05, 1.08, 1.03, 1.05))
        ts += interval

    # Candle 5: local LOW (1.0 low, surrounded by 1.03+ on both sides)
    candles.append(_make_candle(ts, 1.02, 1.04, 1.00, 1.02))
    ts += interval

    # Candles 6-10: rising from 1.05 to 1.25
    for i in range(5):
        price = 1.05 + i * 0.04
        candles.append(_make_candle(ts, price, price + 0.02, price - 0.02, price))
        ts += interval

    # Candles 11-15: continue rising 1.30 to 1.50
    for i in range(5):
        price = 1.30 + i * 0.05
        candles.append(_make_candle(ts, price, price + 0.02, price - 0.02, price))
        ts += interval

    # Candle 16: swing HIGH at 3.0
    candles.append(_make_candle(ts, 2.0, 3.0, 1.95, 2.8))
    ts += interval

    # Candles 17-21: pullback 2.7 → 2.1 (5 candles)
    for i in range(5):
        price = 2.7 - i * 0.15
        candles.append(_make_candle(ts, price, price + 0.05, price - 0.05, price))
        ts += interval

    return candles  # 22 candles total


def _make_gecko(ohlc: list[list] | None = None) -> Any:
    """Return a minimal mock GeckoTerminalClient that returns synthetic OHLC."""
    gecko = AsyncMock()
    gecko.get_token_ohlcv = AsyncMock(return_value=ohlc if ohlc is not None else [])
    return gecko


# =============================================================================
# Test 1: Fib math — swing 2.0/1.0
# =============================================================================

def test_fib_math_swing_2_1() -> None:
    """swing_high=2.0, swing_low=1.0 → verify all 6 fib levels."""
    calc = FibEntryCalculator(gecko=None)
    levels = calc._compute_fib_levels(2.0, 1.0)

    assert set(levels.keys()) == {"0.236", "0.382", "0.5", "0.618", "0.786", "0.886"}
    assert math.isclose(levels["0.236"], 1.764, rel_tol=1e-9)
    assert math.isclose(levels["0.382"], 1.618, rel_tol=1e-9)
    assert math.isclose(levels["0.5"],   1.500, rel_tol=1e-9)
    assert math.isclose(levels["0.618"], 1.382, rel_tol=1e-9)
    assert math.isclose(levels["0.786"], 1.214, rel_tol=1e-9)
    assert math.isclose(levels["0.886"], 1.114, rel_tol=1e-9)


# =============================================================================
# Test 2: Fib math — swing 1.5/1.0
# =============================================================================

def test_fib_math_swing_1_5_1_0() -> None:
    """swing_high=1.5, swing_low=1.0 → range=0.5, verify all 6 levels."""
    calc = FibEntryCalculator(gecko=None)
    levels = calc._compute_fib_levels(1.5, 1.0)

    # range = 0.5
    assert math.isclose(levels["0.236"], 1.5 - 0.5 * 0.236, rel_tol=1e-9)  # 1.382
    assert math.isclose(levels["0.382"], 1.5 - 0.5 * 0.382, rel_tol=1e-9)  # 1.309
    assert math.isclose(levels["0.5"],   1.5 - 0.5 * 0.500, rel_tol=1e-9)  # 1.250
    assert math.isclose(levels["0.618"], 1.5 - 0.5 * 0.618, rel_tol=1e-9)  # 1.191
    assert math.isclose(levels["0.786"], 1.5 - 0.5 * 0.786, rel_tol=1e-9)  # 1.107
    assert math.isclose(levels["0.886"], 1.5 - 0.5 * 0.886, rel_tol=1e-9)  # 1.057


# =============================================================================
# Test 3: Local extrema detection — single clear swing
# =============================================================================

def test_find_swings_detects_single_swing() -> None:
    """
    With the synthetic OHLC (low at 1.0, high at 3.0), _find_swings should
    return a pair where high.price == 3.0 and low.price == 1.0.
    """
    calc = FibEntryCalculator(gecko=None)
    ohlc = _build_synthetic_ohlc()
    result = calc._find_swings(ohlc)

    assert result is not None
    swing_high, swing_low = result
    assert swing_high["price"] == 3.0
    assert swing_low["price"] == 1.0
    # Swing high must come after swing low
    assert swing_high["index"] > swing_low["index"]


# =============================================================================
# Test 4: Current price ABOVE 0.786 → should_wait=True
# =============================================================================

@pytest.mark.asyncio
async def test_compute_current_above_786_wait() -> None:
    """
    Current price (last candle close ≈ 2.125) is well above the 0.786 level (≈1.428).
    should_wait must be True, is_above_target must be True.
    """
    ohlc = _build_synthetic_ohlc()
    calc = FibEntryCalculator(gecko=_make_gecko(ohlc))

    analysis = await calc.compute("TOKEN_A")

    assert analysis is not None
    assert analysis.is_above_target is True
    assert analysis.should_wait is True
    assert analysis.distance_to_target_pct > 0


# =============================================================================
# Test 5: Current price AT 0.786 level → ENTER_NOW (is_above_target=False)
# =============================================================================

@pytest.mark.asyncio
async def test_compute_current_at_786_enter() -> None:
    """
    Build OHLC where last candle close == 0.786 level exactly.
    Swing: low=1.0 (c0), high=3.0 (c10). 0.786 = 3.0 - 2.0*0.786 = 1.428.
    """
    ohlc = _build_synthetic_ohlc()
    # Replace last candle close with exactly the 0.786 level
    target = 3.0 - (3.0 - 1.0) * 0.786  # = 1.428
    last = ohlc[-1]
    ohlc[-1] = _make_candle(last[0], last[1], last[2], last[3], target)

    calc = FibEntryCalculator(gecko=_make_gecko(ohlc))
    analysis = await calc.compute("TOKEN_B")

    assert analysis is not None
    assert analysis.is_above_target is False
    assert analysis.should_wait is False
    assert math.isclose(analysis.current_price, target, rel_tol=1e-9)


# =============================================================================
# Test 6: Current price BELOW 0.786 → is_above_target=False, should_wait=False
# =============================================================================

@pytest.mark.asyncio
async def test_compute_current_below_786_enter() -> None:
    """Current price below target → enter now (still above swing low)."""
    ohlc = _build_synthetic_ohlc()
    # Set last candle close to 1.30 which is below 0.786 level (1.428) but above low (1.0)
    last = ohlc[-1]
    ohlc[-1] = _make_candle(last[0], last[1], last[2], last[3], 1.30)

    calc = FibEntryCalculator(gecko=_make_gecko(ohlc))
    analysis = await calc.compute("TOKEN_C")

    assert analysis is not None
    assert analysis.is_above_target is False
    assert analysis.should_wait is False
    assert analysis.distance_to_target_pct < 0  # below target → negative distance


# =============================================================================
# Test 7: Insufficient swing ratio → None
# =============================================================================

def test_find_swings_insufficient_ratio_returns_none() -> None:
    """
    Build a sequence where the swing high is only 1.3x the low (< 1.5).
    _find_swings must return None.
    """
    base_ts = 1_700_000_000
    candles: list[list] = []
    ts = base_ts

    # 10 rising candles: 1.0 → 1.3
    for i in range(10):
        price = 1.0 + i * 0.03
        candles.append(_make_candle(ts, price, price + 0.01, price - 0.01, price))
        ts += 300

    # 1 high candle at 1.3  (ratio = 1.3 / 1.0 = 1.3 < 1.5)
    candles.append(_make_candle(ts, 1.25, 1.3, 1.24, 1.28))
    ts += 300

    # 5 more pullback candles
    for i in range(5):
        price = 1.2 - i * 0.02
        candles.append(_make_candle(ts, price, price + 0.01, price - 0.01, price))
        ts += 300

    calc = FibEntryCalculator(gecko=None)
    result = calc._find_swings(candles)
    assert result is None


@pytest.mark.asyncio
async def test_compute_insufficient_ratio_returns_none() -> None:
    """compute() must return None when no qualifying swing is found."""
    base_ts = 1_700_000_000
    candles: list[list] = []
    ts = base_ts
    for i in range(20):
        price = 1.0 + i * 0.01
        candles.append(_make_candle(ts, price, price + 0.005, price - 0.005, price))
        ts += 300

    calc = FibEntryCalculator(gecko=_make_gecko(candles))
    analysis = await calc.compute("TOKEN_D")
    assert analysis is None


# =============================================================================
# Test 8: No OHLC data → None
# =============================================================================

@pytest.mark.asyncio
async def test_compute_no_data_returns_none() -> None:
    calc = FibEntryCalculator(gecko=_make_gecko([]))
    result = await calc.compute("TOKEN_E")
    assert result is None


# =============================================================================
# Test 9: Multiple local highs → most recent qualifying swing chosen
# =============================================================================

def test_find_swings_picks_most_recent() -> None:
    """
    Two eligible swings: one earlier (high=4.0), one more recent (high=3.0).
    _find_swings must return the MORE RECENT one (high=3.0).
    """
    base_ts = 1_700_000_000
    interval = 300
    candles: list[list] = []
    ts = base_ts

    def add(high: float, low: float, close: float) -> None:
        nonlocal ts
        candles.append(_make_candle(ts, close, high, low, close))
        ts += interval

    # ---- First swing: low=1.0 at index 0, high=4.0 at index 10 ----
    add(1.05, 1.0, 1.02)         # 0  — local low (1.0)
    for i in range(9):
        p = 1.1 + i * 0.3
        add(p + 0.05, p - 0.05, p)  # 1-9 rising
    add(4.0, 3.8, 3.9)           # 10 — first local high (4.0)

    # ---- Pullback & second base: low=2.0 at index ~16 ----
    for i in range(5):
        p = 3.5 - i * 0.3
        add(p + 0.05, p - 0.05, p)  # 11-15
    add(2.05, 2.0, 2.02)         # 16 — second local low (2.0)
    for i in range(4):
        p = 2.1 + i * 0.25
        add(p + 0.05, p - 0.05, p)  # 17-20

    # ---- Second swing high=3.0 at index 21 (more recent) ----
    add(3.0, 2.8, 2.9)           # 21 — second local high (3.0); ratio 3.0/2.0=1.5 ✓

    # ---- Pullback candles after second high ----
    for i in range(7):
        p = 2.8 - i * 0.1
        add(p + 0.03, p - 0.03, p)  # 22-28

    calc = FibEntryCalculator(gecko=None)
    result = calc._find_swings(candles)

    assert result is not None
    swing_high, swing_low = result
    # The most recent valid high is 3.0
    assert swing_high["price"] == 3.0
    # Its preceding low must be ≥ 2.0 (the second base)
    assert swing_low["price"] >= 2.0


# =============================================================================
# Test 10: suggest_fib_entry — current below target → ENTER_NOW
# =============================================================================

@pytest.mark.asyncio
async def test_suggest_fib_entry_enter_now() -> None:
    """
    Swing: low=1.0, high=3.0. 0.786 level = 1.428.
    current_price = 1.20 (below target AND above low AND drop > 5%).
    Expected: ENTER_NOW.
    """
    ohlc = _build_synthetic_ohlc()
    calc = FibEntryCalculator(gecko=_make_gecko(ohlc))

    rec, target_price, reasoning = await calc.suggest_fib_entry(
        token_address="TOKEN_F",
        current_price=1.20,
        min_drop_pct=5.0,
    )

    assert rec == ENTER_NOW
    assert math.isclose(target_price, 1.428, rel_tol=1e-9)
    assert "Enter now" in reasoning or "entry zone" in reasoning.lower()


# =============================================================================
# Test 11: suggest_fib_entry — current above target → WAIT_FOR_DIP
# =============================================================================

@pytest.mark.asyncio
async def test_suggest_fib_entry_wait_for_dip() -> None:
    """
    current_price = 2.0 (above 0.786 level = 1.428, but has dropped enough).
    Expected: WAIT_FOR_DIP.
    """
    ohlc = _build_synthetic_ohlc()
    calc = FibEntryCalculator(gecko=_make_gecko(ohlc))

    rec, target_price, reasoning = await calc.suggest_fib_entry(
        token_address="TOKEN_G",
        current_price=2.0,
        min_drop_pct=5.0,
    )

    assert rec == WAIT_FOR_DIP
    assert math.isclose(target_price, 1.428, rel_tol=1e-9)


# =============================================================================
# Test 12: suggest_fib_entry — no gecko data → NO_DATA
# =============================================================================

@pytest.mark.asyncio
async def test_suggest_fib_entry_no_data() -> None:
    calc = FibEntryCalculator(gecko=_make_gecko([]))

    result = await calc.suggest_fib_entry("TOKEN_H", current_price=1.5)

    assert result is not None
    rec, _, reasoning = result
    assert rec == NO_DATA
    assert "Insufficient" in reasoning


# =============================================================================
# Test 13: suggest_fib_entry — price below swing low → OUT_OF_RANGE
# =============================================================================

@pytest.mark.asyncio
async def test_suggest_fib_entry_out_of_range() -> None:
    """
    Swing low = 1.0. current_price = 0.80 (below low) → OUT_OF_RANGE.
    """
    ohlc = _build_synthetic_ohlc()
    calc = FibEntryCalculator(gecko=_make_gecko(ohlc))

    rec, target_price, reasoning = await calc.suggest_fib_entry(
        token_address="TOKEN_I",
        current_price=0.80,
    )

    assert rec == OUT_OF_RANGE
    assert "below swing low" in reasoning.lower()


# =============================================================================
# Test 14: suggest_fib_entry — price not dropped enough → WAIT_FOR_DIP
# =============================================================================

@pytest.mark.asyncio
async def test_suggest_fib_entry_not_dropped_enough() -> None:
    """
    Swing high = 3.0. current_price = 2.98 (only ~0.67% drop, < 5% min_drop_pct).
    Must return WAIT_FOR_DIP even though price might be near or below 0.786 level
    (it won't be at 2.98, but the min_drop check fires first).
    """
    ohlc = _build_synthetic_ohlc()
    calc = FibEntryCalculator(gecko=_make_gecko(ohlc))

    rec, _, reasoning = await calc.suggest_fib_entry(
        token_address="TOKEN_J",
        current_price=2.98,
        min_drop_pct=5.0,
    )

    assert rec == WAIT_FOR_DIP
    assert "below swing high" in reasoning.lower() or "pullback" in reasoning.lower()
