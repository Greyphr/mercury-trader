"""Hermes pre-trade assessment coverage via the rule-based client (no LLM API).

Asserts the published ``signal.assessed`` assessment has the shape the risk
manager consumes, and that rule-based assessments fail closed by default
(Critical Fix #4) — confidence 0.6 no longer passes the gate on its own.
"""

import pytest

from mercury.core.events import Event, EventBus
from mercury.models.orm import ReasoningRecord
from mercury.models.schemas import Direction, ReasoningKind, Signal, SignalSource
from mercury.services.hermes.service import HermesService
from mercury.services.risk.service import RiskManagerService


def _signal():
    return Signal(
        provider=SignalSource.INTERNAL_STRATEGY,
        strategy_id="xauusd_m5_trend",
        symbol="XAUUSD",
        timeframe="M5",
        direction=Direction.LONG,
        price=2400.0,
        sl=2395.0,
        tp=2412.0,
    )


def _hermes(settings, db, bus=None):
    settings.providers.llm.mode = "none"
    return HermesService(bus=bus or EventBus(), settings=settings, db=db)


def _risk_service(settings, db, bus=None):
    settings.risk.guards.session_check = False
    settings.risk.guards.news_blackout_minutes = 0
    svc = RiskManagerService(bus=bus or EventBus(), settings=settings, db=db)
    svc.set_equity_provider(lambda: 10000.0)
    return svc


async def _assess(hermes, bus):
    collected: list[Event] = []
    bus.subscribe("signal.assessed", lambda e: collected.append(e))
    await bus.publish(
        Event("signal.validated", {"signal": _signal(), "signal_id": 1})
    )
    assert len(collected) == 1
    return collected[0]


@pytest.mark.asyncio
async def test_on_signal_validated_publishes_rule_based_assessment(settings, db):
    bus = EventBus()
    hermes = _hermes(settings, db, bus=bus)
    await hermes.start()

    event = await _assess(hermes, bus)

    assert event.topic == "signal.assessed"
    assert event.payload["signal_id"] == 1
    assert event.payload["signal"] is not None

    assessment = event.payload["assessment"]
    for key in (
        "decision",
        "confidence",
        "summary",
        "market_conditions",
        "risks",
        "supporting_factors",
        "notes",
    ):
        assert key in assessment
    assert assessment["decision"] == "proceed"
    assert assessment["confidence"] == pytest.approx(0.6)
    assert assessment["provider"] == "rule_based"
    await hermes.stop()


@pytest.mark.asyncio
async def test_assessment_persisted_as_reasoning_record(settings, db):
    hermes = _hermes(settings, db)
    await hermes.start()

    await hermes.on_signal_validated(
        Event("signal.validated", {"signal": _signal(), "signal_id": 2})
    )

    with db.session() as session:
        rows = session.query(ReasoningRecord).all()
    assert len(rows) == 1
    record = rows[0]
    assert record.kind == ReasoningKind.PRE_TRADE.value
    assert record.signal_id == 2
    assert record.provider == "rule_based"
    assert record.confidence == pytest.approx(0.6)
    assert record.structured["provider"] == "rule_based"
    await hermes.stop()


@pytest.mark.asyncio
async def test_rule_based_assessment_fails_closed_by_default(settings, db):
    hermes = _hermes(settings, db)
    await hermes.start()
    event = await _assess(hermes, hermes.bus)

    assert settings.risk.guards.allow_rule_based_trading is False
    svc = _risk_service(settings, db)
    decision = svc.evaluate(event.payload["signal"], event.payload["assessment"])

    assert not decision.approved
    assert any("rule-based" in r.lower() for r in decision.reasons)
    assert not any("confidence" in r and "<" in r for r in decision.reasons)
    await hermes.stop()


@pytest.mark.asyncio
async def test_rule_based_assessment_allowed_when_flag_enabled(settings, db):
    settings.risk.guards.allow_rule_based_trading = True
    hermes = _hermes(settings, db)
    await hermes.start()
    event = await _assess(hermes, hermes.bus)

    svc = _risk_service(settings, db)
    decision = svc.evaluate(event.payload["signal"], event.payload["assessment"])

    assert decision.approved
    assert decision.volume > 0
    await hermes.stop()


@pytest.mark.asyncio
async def test_signal_validated_to_signal_rejected_pipeline(settings, db):
    bus = EventBus()
    hermes = _hermes(settings, db, bus=bus)
    risk = _risk_service(settings, db, bus=bus)
    await hermes.start()
    await risk.start()

    rejected: list[Event] = []
    bus.subscribe("signal.rejected", lambda e: rejected.append(e))

    await bus.publish(Event("signal.validated", {"signal": _signal(), "signal_id": 3}))

    assert len(rejected) == 1
    assert rejected[0].payload["signal_id"] == 3
    assert any("rule-based" in r.lower() for r in rejected[0].payload["reasons"])
    await hermes.stop()
    await risk.stop()


@pytest.mark.asyncio
async def test_validated_event_without_signal_is_ignored(settings, db):
    bus = EventBus()
    hermes = _hermes(settings, db, bus=bus)
    await hermes.start()

    collected: list[Event] = []
    bus.subscribe("signal.assessed", lambda e: collected.append(e))

    await bus.publish(Event("signal.validated", {"signal_id": 99}))
    assert collected == []
    await hermes.stop()
