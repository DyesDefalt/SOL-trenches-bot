"""
Pydantic V2 models untuk semua structured LLM outputs.

Setiap schema di sini merepresentasikan satu jenis response dari LLM.
LLMClient.complete_structured() akan parse JSON response ke salah satu model ini.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RugCheckResult(BaseModel):
    """Pre-trade rug check result dari RugCheckAgent."""

    veto: bool
    confidence: float = Field(ge=0, le=1)
    reason: str
    red_flags: list[str] = []
    recommendation: Literal["APPROVE", "VETO", "REDUCE_SIZE"]


class TradeReflection(BaseModel):
    """Post-trade reflection result dari ReflectionAgent."""

    trade_classification: Literal[
        "PROFITABLE_AS_EXPECTED",
        "LUCKY_PROFIT",
        "UNPROFITABLE_AS_EXPECTED",
        "UNEXPECTED_LOSS",
    ]
    key_signals: list[str]
    misleading_signals: list[str] = []
    emerging_pattern: str | None = None
    recommended_adjustment: dict | None = None
    lesson_summary: str


class WalletAssessment(BaseModel):
    """Smart wallet assessment — dipakai di wallet analyzer agent (future)."""

    classification: Literal[
        "ALPHA_TRADER",
        "WASH_TRADER",
        "SCALPER",
        "POSITION_TRADER",
        "REGIME_DEPENDENT",
        "UNCLEAR",
    ]
    confidence: float = Field(ge=0, le=1)
    compatible_with_bot: bool
    avg_hold_minutes: int
    diversification_score: int = Field(ge=0, le=10)
    recommended_tier: Literal["A", "B", "C", "BLACKLIST", "F"]
    reason: str


class TunerRecommendation(BaseModel):
    """Parameter tuning recommendation dari TunerAgent (future, Phase 6b)."""

    parameter: str
    current_value: float
    suggested_value: float
    justification: str
    expected_impact: str
    confidence: float = Field(ge=0, le=1)
    warning_flags: list[str] = []


class MemeQualityScore(BaseModel):
    """LLM-evaluated meme quality. Phase 10.6."""

    overall_score: int = Field(ge=0, le=100, description="Overall quality 0-100")
    visual_clarity: int = Field(ge=0, le=10)
    memetic_potential: int = Field(ge=0, le=10)
    cultural_fit: int = Field(ge=0, le=10)
    originality: int = Field(ge=0, le=10)
    community_signal: int = Field(ge=0, le=10)
    is_clone: bool = Field(description="Likely copycat of a trending token?")
    cultural_reference: str = Field(
        default="", description="What zeitgeist/meme it references, if any"
    )
    risks: list[str] = Field(
        default_factory=list, description="Specific risks identified"
    )
    reasoning: str = Field(description="Brief one-paragraph explanation")
