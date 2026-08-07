"""Unit tests for StrategyEngineService's live-signal emission path.

generate_signals returns every historical match (full-history scan, required
for backtests); the engine must only emit signals tied to the newest closed
candle on the live path, plus dedupe repeats within the same candle.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from mercury.core.events import Event, EventBus
from mercury.core.validation import Candle
from mercury.models.schemas import Direction, Signal, SignalSource
from mercury.services.strategy.engine import StrategyEngineService


def _candle(t: datetime) -> Candle:
    return Candle(
        symbol="GOLD", timeframe="M5", time=t,
        open=100.0, high=110.0, low=95.0, close=105.0, volume=100,
    )


def _row(c: Candle) -> dict:
    return {
        "symbol": c.symbol,
        "timeframe": c.timeframe,
        "time": c.time,
        "open": c.open,
        "high": c.high,
        "low": c.low,
        "close": c.close,
        "volume": c.volume,
    }


def _signal(candle_time: datetime, direction: Direction) -> Signal:
    return Signal(
        provider=SignalSource.INTERNAL_STRATEGY,
        strategy_id="stub",
        symbol="GOLD",
        timeframe="M5",
        direction=direction,
        price=105.0,
        meta={"candle_time": candle_time.isoformat()},
    )


class _StubStrategy:
    def __init__(self, signals: list[Signal]) -> None:
        self.config = SimpleNamespace(symbol="GOLD", timeframe="M5")
        self.signals = signals

    def generate_signals(self, candles: list[Candle]) -> list[Signal]:
        return self.signals


def _service(settings, db, signals) -> tuple[StrategyEngineService, list[Event]]:
    bus = EventBus()
    received: list[Event] = []
    bus.subscribe("signal.received", lambda e: received.append(e))
    svc = StrategyEngineService(bus=bus, settings=settings, db=db)
    svc._strategies = [_StubStrategy(signals)]
    return svc, received


def test_run_for_emits_only_newest_closed_candle_signals(settings, db, monkeypatch):
    t = datetime.now(UTC)
    c0 = _candle(t - timedelta(minutes=10))
    c1 = _candle(t - timedelta(minutes=5))
    c2 = _candle(t)  # in-progress candle — excluded from `closed`
    monkeypatch.setattr(
        "mercury.services.data.historical.load_history_from_db",
        lambda db, symbol, timeframe, count=1000: [_row(c) for c in (c0, c1, c2)],
    )

    signals = [
        _signal(c0.time, Direction.LONG),   # historical match → dropped
        _signal(c1.time, Direction.SHORT),  # newest closed candle → emitted
        _signal(c2.time, Direction.LONG),   # in-progress candle → dropped
    ]
    svc, received = _service(settings, db, signals)

    svc._run_for("GOLD", "M5")

    assert len(received) == 1
    assert received[0].payload.meta["candle_time"] == c1.time.isoformat()
    assert received[0].payload.direction == Direction.SHORT


def test_run_for_dedups_repeat_calls_within_same_candle(settings, db, monkeypatch):
    t = datetime.now(UTC)
    c0 = _candle(t - timedelta(minutes=10))
    c1 = _candle(t - timedelta(minutes=5))
    monkeypatch.setattr(
        "mercury.services.data.historical.load_history_from_db",
        lambda db, symbol, timeframe, count=1000: [_row(c) for c in (c0, c1)],
    )

    svc, received = _service(settings, db, [_signal(c0.time, Direction.LONG)])

    svc._run_for("GOLD", "M5")
    svc._run_for("GOLD", "M5")

    assert len(received) == 1


def test_unmatched_candle_time_dropped_with_debug_log(settings, db, monkeypatch, caplog):
    t = datetime.now(UTC)
    c0 = _candle(t - timedelta(minutes=10))
    c1 = _candle(t - timedelta(minutes=5))
    monkeypatch.setattr(
        "mercury.services.data.historical.load_history_from_db",
        lambda db, symbol, timeframe, count=1000: [_row(c) for c in (c0, c1)],
    )

    ghost = datetime.now(UTC) - timedelta(hours=1)
    svc, received = _service(settings, db, [_signal(ghost, Direction.LONG)])

    with caplog.at_level(logging.DEBUG, logger="mercury.services.strategy.engine"):
        svc._run_for("GOLD", "M5")

    assert received == []
    assert any("dropping stale signal" in r.message for r in caplog.records)
