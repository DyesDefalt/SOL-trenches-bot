"""
Tests for AICostTracker — daily spend tracking dengan circuit breaker.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from src.ai.cost_tracker import AICostTracker, _PRICING


class TestAICostTracker:
    """Unit tests untuk AICostTracker."""

    def test_initial_state(self):
        """Fresh tracker starts at zero spend, can proceed."""
        tracker = AICostTracker()
        assert tracker.daily_spend_usd() == 0.0
        assert tracker.can_proceed() is True

    def test_cost_calculation_gemini_flash(self):
        """Cost calculation menggunakan pricing table yang benar."""
        tracker = AICostTracker()
        model = "google/gemini-2.0-flash"
        # 1M input tokens = $0.10, 1M output tokens = $0.40
        cost = tracker.record(model, input_tokens=1_000_000, output_tokens=1_000_000)
        expected = 0.10 + 0.40
        assert abs(cost - expected) < 1e-9
        assert abs(tracker.daily_spend_usd() - expected) < 1e-9

    def test_cost_calculation_claude_haiku(self):
        """Claude Haiku pricing: $1.00 in / $5.00 out per 1M tokens."""
        tracker = AICostTracker()
        model = "anthropic/claude-haiku-4.5"
        # 500 input tokens + 200 output tokens
        cost = tracker.record(model, input_tokens=500, output_tokens=200)
        expected = (500 / 1_000_000) * 1.00 + (200 / 1_000_000) * 5.00
        assert abs(cost - expected) < 1e-10

    def test_cap_enforcement_blocks_proceed(self):
        """can_proceed() returns False ketika spend sudah >= cap."""
        tracker = AICostTracker()
        # Set spend tepat di cap via monkeypatching
        with patch.object(tracker, "_daily_spend", 1.00):
            with patch("src.ai.cost_tracker.settings") as mock_settings:
                mock_settings.llm_daily_cost_cap_usd = 1.00
                assert tracker.can_proceed() is False

    def test_reset_daily_clears_spend(self):
        """reset_daily() zero-out spend."""
        tracker = AICostTracker()
        tracker.record("google/gemini-2.0-flash", 1_000_000, 1_000_000)
        assert tracker.daily_spend_usd() > 0
        tracker.reset_daily()
        assert tracker.daily_spend_usd() == 0.0

    def test_midnight_auto_reset(self):
        """Spend di-reset kalau hari berganti (midnight UTC)."""
        tracker = AICostTracker()
        tracker.record("google/gemini-2.0-flash", 1_000_000, 1_000_000)
        assert tracker.daily_spend_usd() > 0

        # Simulasi hari berganti
        tracker._current_day = "2020-01-01"  # past date
        # Calling daily_spend_usd() triggers _maybe_reset
        spend = tracker.daily_spend_usd()
        assert spend == 0.0  # Reset karena hari berbeda

    def test_concurrent_record_async(self):
        """Concurrent record_async() calls tidak corrupting state."""
        async def run():
            tracker = AICostTracker()
            model = "google/gemini-2.0-flash"
            # Fire 10 concurrent record calls
            tasks = [
                tracker.record_async(model, 1000, 500)
                for _ in range(10)
            ]
            costs = await asyncio.gather(*tasks)
            # Total cost harus sama dengan 10x individual cost
            single_cost = (1000 / 1_000_000) * 0.10 + (500 / 1_000_000) * 0.40
            expected_total = single_cost * 10
            assert abs(tracker.daily_spend_usd() - expected_total) < 1e-9
            assert len(costs) == 10

        asyncio.run(run())

    def test_unknown_model_uses_fallback_pricing(self):
        """Model yang tidak dikenal menggunakan fallback pricing."""
        tracker = AICostTracker()
        # Model yang tidak ada di _PRICING table
        cost = tracker.record("unknown/model-xyz", input_tokens=1_000_000, output_tokens=0)
        # Fallback in = $1.00 / 1M
        assert abs(cost - 1.00) < 1e-9
