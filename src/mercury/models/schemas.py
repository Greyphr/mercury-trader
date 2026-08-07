"""Pydantic domain schemas shared across services.

These are transport/validation models — NOT persisted objects. Persistence is
handled by the SQLAlchemy ORM models in :mod:`mercury.models.orm`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


# ── Enums ─────────────────────────────────────────────────────
class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"


class SignalSource(StrEnum):
    INTERNAL_STRATEGY = "internal_strategy"
    TRADINGVIEW = "tradingview"
    MANUAL = "manual"


class TradeStatus(StrEnum):
    PENDING = "pending"
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    MANUAL_REVIEW = "manual_review"


class CloseReason(StrEnum):
    TAKE_PROFIT = "tp"
    STOP_LOSS = "sl"
    BREAKEVEN = "breakeven"
    OPPOSITE_BOS = "bos"
    MANUAL = "manual"
    KILL_SWITCH = "kill_switch"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


class ReasoningKind(StrEnum):
    PRE_TRADE = "pre_trade"
    POST_TRADE = "post_trade"
    DAILY = "daily"
    WEEKLY = "weekly"


class ProposalStatus(StrEnum):
    PROPOSED = "proposed"
    BACKTESTING = "backtesting"
    BACKTESTED = "backtested"
    AWAITING_HUMAN = "awaiting_human"
    APPROVED_PAPER = "approved_paper"
    PAPER_RUNNING = "paper_running"
    PROMOTED = "promoted"
    REJECTED = "rejected"


class StrategyStage(StrEnum):
    """Deployment lifecycle stage of a strategy.

    A strategy must be explicitly promoted one stage at a time:
    draft → paper → demo → review → approved → live. Live trading is
    blocked until the strategy reaches ``approved`` (explicit human gate).
    """

    DRAFT = "draft"
    PAPER = "paper"
    DEMO = "demo"
    REVIEW = "review"
    APPROVED = "approved"
    LIVE = "live"


# ── Market ────────────────────────────────────────────────────
class Quote(BaseModel):
    symbol: str
    bid: float
    ask: float
    spread_points: float = 0.0
    time: datetime


# ── Signal ────────────────────────────────────────────────────
class Signal(BaseModel):
    provider: SignalSource
    strategy_id: str | None = None
    symbol: str
    timeframe: str
    direction: Direction
    price: float | None = None
    sl: float | None = None
    tp: float | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ── Hermes reasoning ──────────────────────────────────────────
class PreTradeAssessment(BaseModel):
    """Structured pre-trade reasoning produced by Hermes."""

    decision: Literal["proceed", "abstain"] = "abstain"
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str = ""
    market_conditions: dict[str, Any] = Field(default_factory=dict)
    risks: list[str] = Field(default_factory=list)
    supporting_factors: list[str] = Field(default_factory=list)
    notes: str = ""


class PostTradeReview(BaseModel):
    """Structured post-trade review produced by Hermes."""

    outcome_assessment: str = ""
    factors: list[str] = Field(default_factory=list)
    comparison: dict[str, Any] = Field(default_factory=dict)
    lessons: list[str] = Field(default_factory=list)
    actionable_recommendations: list[str] = Field(default_factory=list)


class DailyAnalysis(BaseModel):
    """Structured daily analysis produced by Hermes."""

    market_summary: str = ""
    performance_review: dict[str, Any] = Field(default_factory=dict)
    patterns_identified: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    opportunities: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    proposals: list[dict[str, Any]] = Field(default_factory=list)


# ── Proposals ─────────────────────────────────────────────────
class BacktestResult(BaseModel):
    strategy_id: str
    start: datetime
    end: datetime
    trades: int
    win_rate: float
    profit_factor: float
    net_profit: float
    max_drawdown_percent: float
    expectancy_r: float
    metrics: dict[str, Any] = Field(default_factory=dict)


class ImprovementProposal(BaseModel):
    source: str = "hermes"
    hypothesis: str
    description: str
    proposed_config: dict[str, Any] = Field(default_factory=dict)
    status: ProposalStatus = ProposalStatus.PROPOSED
