"""SQLAlchemy ORM models (persistence layer).

Every trade, signal, reasoning output, proposal, and metric snapshot is
persisted here so the learning pipeline can analyze history over time.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mercury.core.db import Base


class _Timestamps:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SignalRecord(Base):
    """A raw signal received from any provider."""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    strategy_id: Mapped[str | None] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    timeframe: Mapped[str] = mapped_column(String(8))
    direction: Mapped[str] = mapped_column(String(8))
    price: Mapped[float | None]
    sl: Mapped[float | None]
    tp: Mapped[float | None]
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CandleRecord(Base):
    """Stored OHLCV candle per symbol/timeframe."""

    __tablename__ = "candles"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    timeframe: Mapped[str] = mapped_column(String(8))
    time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0.0)

    __table_args__ = (UniqueConstraint("symbol", "timeframe", "time", name="uq_candle"),)


class TradeRecord(Base):
    """A persisted trade (paper or live)."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"))
    ticket: Mapped[str | None] = mapped_column(String(32), unique=True, index=True)
    strategy_id: Mapped[str | None] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    direction: Mapped[str] = mapped_column(String(8))
    volume: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    sl: Mapped[float | None]
    tp: Mapped[float | None]
    status: Mapped[str] = mapped_column(String(16), index=True)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, server_default=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    close_price: Mapped[float | None]
    close_reason: Mapped[str | None] = mapped_column(String(16), index=True)
    risk_amount: Mapped[float] = mapped_column(Float, default=0.0)
    risk_r: Mapped[float] = mapped_column(Float, default=0.0)
    pnl: Mapped[float] = mapped_column(Float, default=0.0)
    pnl_r: Mapped[float] = mapped_column(Float, default=0.0)
    spread_points_at_entry: Mapped[float] = mapped_column(Float, default=0.0)
    session_name: Mapped[str | None] = mapped_column(String(32))
    deployment_mode: Mapped[str] = mapped_column(String(16))
    pre_trade_confidence: Mapped[float | None]
    meta: Mapped[dict] = mapped_column(JSON, default=dict)

    signal: Mapped[SignalRecord | None] = relationship()
    reasonings: Mapped[list[ReasoningRecord]] = relationship(back_populates="trade")


class ReasoningRecord(Base):
    """Structured reasoning output from Hermes."""

    __tablename__ = "reasonings"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    trade_id: Mapped[int | None] = mapped_column(ForeignKey("trades.id"), index=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(64))
    confidence: Mapped[float | None]
    summary: Mapped[str] = mapped_column(Text, default="")
    structured: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    trade: Mapped[TradeRecord | None] = relationship(back_populates="reasonings")


class ProposalRecord(Base):
    """A Hermes strategy-improvement proposal tracked through the
    backtest → paper → human-approval → live pipeline."""

    __tablename__ = "proposals"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), default="hermes")
    hypothesis: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    proposed_config: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), index=True)
    backtest_result: Mapped[dict | None] = mapped_column(JSON)
    review_notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StrategyVersionRecord(Base):
    """A deployed strategy version (paper or live)."""

    __tablename__ = "strategy_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer)
    config: Mapped[dict] = mapped_column(JSON)
    stage: Mapped[str] = mapped_column(String(16))  # backtest | paper | live
    proposal_id: Mapped[int | None] = mapped_column(ForeignKey("proposals.id"))
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("strategy_id", "version", name="uq_strategy_version"),)


class NewsEventRecord(Base):
    """A collected news/economic event used for blackouts and reasoning."""

    __tablename__ = "news_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    impact: Mapped[str | None] = mapped_column(String(16), index=True)
    currency: Mapped[str | None] = mapped_column(String(8), index=True)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    sentiment_score: Mapped[float | None]
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MetricsRecord(Base):
    """A periodic snapshot of performance metrics (daily/weekly/monthly)."""

    __tablename__ = "metrics_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    period: Mapped[str] = mapped_column(String(16), index=True)  # daily | weekly | monthly
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    metrics: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SystemStateRecord(Base):
    """Single-row state table (kill switch, last scheduled runs, etc.)."""

    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class StrategyLifecycleRecord(Base):
    """DB-tracked promotion stage of a strategy (draft → paper → demo →
    review → approved → live). One row per strategy; the stage gates where the
    strategy may trade."""

    __tablename__ = "strategy_lifecycle"

    strategy_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    stage: Mapped[str] = mapped_column(String(16), index=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class StrategyPromotionRecord(Base):
    """Audit log of every strategy promotion transition."""

    __tablename__ = "strategy_promotions"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[str] = mapped_column(String(64), index=True)
    from_stage: Mapped[str] = mapped_column(String(16))
    to_stage: Mapped[str] = mapped_column(String(16))
    actor: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(512), default="")
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
