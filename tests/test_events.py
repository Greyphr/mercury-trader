import asyncio

from mercury.core.events import Event, EventBus


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
