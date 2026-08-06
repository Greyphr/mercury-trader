"""In-process event bus.

Services communicate through typed events. The bus supports both sync and
async handlers. This is a light, dependency-free implementation that can be
swapped for Redis/Kafka later without touching service code.

Event topics are dotted namespaces: ``trade.opened``, ``signal.received``, etc.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mercury.core.logging import get_logger

logger = get_logger("core.events")

Handler = Callable[[Any], Any]


@dataclass(slots=True)
class Event:
    """A domain event published on the bus."""

    topic: str
    payload: Any = None
    occurred_at: float = field(default_factory=time.time)


class EventBus:
    """Topic-based pub/sub dispatcher.

    - ``publish`` (async) awaits handlers.
    - ``publish_nowait`` schedules handlers without awaiting (fire-and-forget).
    - Handlers may be sync or async; exceptions are logged, never propagated.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Handler]] = defaultdict(list)
        self._wildcard: list[Handler] = []
        self._lock = threading.RLock()

    # ── subscription ──────────────────────────────────────────
    def subscribe(self, topic: str, handler: Handler) -> None:
        """Register a handler for a topic (exact match)."""
        with self._lock:
            self._subscribers[topic].append(handler)
        logger.debug("subscribed", extra={"topic": topic, "handler": getattr(handler, "__name__", str(handler))})

    def subscribe_wildcard(self, handler: Handler) -> None:
        """Register a handler for every topic."""
        with self._lock:
            self._wildcard.append(handler)

    def unsubscribe(self, topic: str, handler: Handler) -> None:
        with self._lock:
            if handler in self._subscribers.get(topic, []):
                self._subscribers[topic].remove(handler)

    # ── dispatch ──────────────────────────────────────────────
    def _handlers_for(self, topic: str) -> list[Handler]:
        with self._lock:
            return [*self._wildcard, *self._subscribers.get(topic, [])]

    async def publish(self, event: Event) -> None:
        """Dispatch to all handlers, awaiting async ones."""
        handlers = self._handlers_for(event.topic)
        for handler in handlers:
            await self._invoke(handler, event)

    def publish_nowait(self, event: Event) -> None:
        """Fire-and-forget dispatch; async handlers scheduled on the loop."""
        handlers = self._handlers_for(event.topic)
        loop = _current_or_running_loop()
        for handler in handlers:
            if inspect.iscoroutinefunction(handler):
                if loop is not None:
                    loop.create_task(self._invoke(handler, event))
                else:
                    logger.warning("no running loop, dropping async handler", extra={"topic": event.topic})
            else:
                self._safe_call(handler, event)

    async def _invoke(self, handler: Handler, event: Event) -> None:
        if inspect.iscoroutinefunction(handler):
            try:
                await handler(event)
            except Exception:  # noqa: BLE001
                logger.exception("async event handler failed", extra={"topic": event.topic})
        else:
            self._safe_call(handler, event)

    def _safe_call(self, handler: Handler, event: Event) -> None:
        try:
            handler(event)
        except Exception:  # noqa: BLE001
            logger.exception("event handler failed", extra={"topic": event.topic})


def _current_or_running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None
