import pytest

from mercury.core.events import EventBus
from mercury.models.schemas import Direction, Signal, SignalSource
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


def _risk_service(settings, db, *, equity=10000.0, session_check=True):
    settings.risk.guards.session_check = session_check
    settings.risk.guards.news_blackout_minutes = 0
    svc = RiskManagerService(bus=EventBus(), settings=settings, db=db)
    svc.set_equity_provider(lambda: equity)
    return svc


@pytest.mark.asyncio
async def test_confident_signal_approved(settings, db):
    svc = _risk_service(settings, db)
    decision = svc.evaluate(_signal(), {"confidence": 0.9})
    assert decision.approved
    assert decision.volume > 0
    assert decision.risk_amount > 0


@pytest.mark.asyncio
async def test_low_confidence_rejected(settings, db):
    svc = _risk_service(settings, db)
    decision = svc.evaluate(_signal(), {"confidence": 0.1})
    assert not decision.approved
    assert any("confidence" in r for r in decision.reasons)


@pytest.mark.asyncio
async def test_outside_session_rejected(settings, db):
    svc = _risk_service(settings, db, session_check=True)
    # Force a session that never matches: swap to an empty session list.
    settings.base.trading_sessions = []
    decision = svc.evaluate(_signal(), {"confidence": 0.9})
    assert not decision.approved
    assert any("session" in r for r in decision.reasons)


@pytest.mark.asyncio
async def test_spread_filter(settings, db):
    svc = _risk_service(settings, db)
    svc._last_quote = {"symbol": "XAUUSD", "bid": 2399.0, "ask": 2401.0, "spread_points": 200}
    decision = svc.evaluate(_signal(), {"confidence": 0.9})
    assert not decision.approved
    assert any("spread" in r for r in decision.reasons)


@pytest.mark.asyncio
async def test_position_size_risk_scales_with_equity(settings, db):
    svc = _risk_service(settings, db, equity=10000.0)
    d1 = svc.evaluate(_signal(), {"confidence": 0.9})

    svc2 = _risk_service(settings, db, equity=20000.0)
    d2 = svc2.evaluate(_signal(), {"confidence": 0.9})
    assert d2.volume == pytest.approx(d1.volume * 2, rel=0.05)
