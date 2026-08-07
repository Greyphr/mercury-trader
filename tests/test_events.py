import asyncio
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import select

from mercury.core.events import Event, EventBus
from mercury.models.orm import EventRecord


def test_subscribe_and_publish():
    bus = EventBus()
    received = []

    def handler(event):
        received.append(event.payload)

    bus.subscribe("test.topic", handler)
    bus.publish_nowait(Event("test.topic", {"x": 1}))
    assert received == [{"x": 1}]


def test_async_handler_awaited():
    bus = EventBus()
    received = []

    async def handler(event):
        await asyncio.sleep(0)
        received.append(event.payload)

    async def main():
        bus.subscribe("test.async", handler)
        await bus.publish(Event("test.async", 42))

    asyncio.run(main())
    assert received == [42]


def test_wildcard_subscription():
    bus = EventBus()
    topics = []
    bus.subscribe_wildcard(lambda e: topics.append(e.topic))
    bus.publish_nowait(Event("a.b", None))
    bus.publish_nowait(Event("c.d", None))
    assert topics == ["a.b", "c.d"]


def test_handler_errors_logged_not_raised():
    bus = EventBus()

    def bad(event):
        raise RuntimeError("boom")

    def good(event):
        pass

    bus.subscribe("x", bad)
    bus.subscribe("x", good)
    bus.publish_nowait(Event("x", None))  # should not raise


def _audit_rows(db):
    with db.session() as session:
        return list(session.scalars(select(EventRecord)))


def test_publish_audits_allowlisted_topic(db):
    bus = EventBus(db=db, audit_topics=["trade.opened"])

    asyncio.run(bus.publish(Event("trade.opened", {"ticket": "T1"})))

    rows = _audit_rows(db)
    assert [r.topic for r in rows] == ["trade.opened"]
    assert rows[0].payload == {"ticket": "T1"}


def test_publish_nowait_audits_allowlisted_topic(db):
    bus = EventBus(db=db, audit_topics=["trade.closed", "system.critical"])
    bus.publish_nowait(Event("trade.closed", {"ticket": "T2", "pnl": 1.5}))
    bus.publish_nowait(Event("system.critical", {"reason": "kill switch"}))

    rows = _audit_rows(db)
    assert [r.topic for r in rows] == ["trade.closed", "system.critical"]
    assert rows[1].payload == {"reason": "kill switch"}


def test_publish_skips_non_allowlisted_topic(db):
    bus = EventBus(db=db, audit_topics=["trade.opened"])
    bus.publish_nowait(Event("market.data.updated", {"symbol": "XAUUSD"}))

    assert _audit_rows(db) == []


def test_bus_without_db_does_not_audit(db):
    bus = EventBus()  # no db attached
    bus.publish_nowait(Event("trade.opened", {"ticket": "T3"}))

    assert _audit_rows(db) == []


def test_audit_payload_is_json_serializable(db):
    bus = EventBus(db=db, audit_topics=["signal.approved"])
    bus.publish_nowait(
        Event(
            "signal.approved",
            {"signal": SimpleNamespace(strategy_id="ict", price=2000.0), "at": datetime(2026, 8, 7, 12, 0, 0)},
        )
    )

    rows = _audit_rows(db)
    assert rows[0].payload == {"signal": {"strategy_id": "ict", "price": 2000.0}, "at": "2026-08-07T12:00:00"}
