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
from datetime import datetime
from typing import Any

from mercury.core.logging import get_logger

logger = get_logger("core.events")

Handler = Callable[[Any], Any]

_JSON_SAFE = (int, float, str, bool, type(None))


def _jsonable(obj: Any) -> Any:
    """Coerce arbitrary event payloads into JSON-safe primitives."""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, _JSON_SAFE):
        return obj
    if hasattr(obj, "model_dump"):
        return _jsonable(obj.model_dump(mode="json"))
    try:
        return _jsonable(vars(obj))
    except TypeError:
        return str(obj)


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

    When a ``db`` and ``audit_topics`` are supplied, allowlisted events are
    written to the ``event_audit`` table before dispatch so a crash
    mid-dispatch still leaves a record. Audit writes are best-effort and never
    affect dispatch.
    """

    def __init__(self, *, db: Any = None, audit_topics: tuple[str, ...] | list[str] = ()) -> None:
        self._subscribers: dict[str, list[Handler]] = defaultdict(list)
        self._wildcard: list[Handler] = []
        self._lock = threading.RLock()
        self._db = db
        self._audit_topics = set(audit_topics)

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

    def _audit(self, event: Event) -> None:
        """Persist allowlisted events to the audit log (best-effort)."""
        if self._db is None or event.topic not in self._audit_topics:
            return
        from mercury.models.orm import EventRecord

        try:
            with self._db.session() as session:
                session.add(
                    EventRecord(
                        topic=event.topic,
                        payload=_jsonable(event.payload),
                        occurred_at=event.occurred_at,
                    )
                )
        except Exception:  # noqa: BLE001
            logger.warning("event audit write failed", extra={"topic": event.topic}, exc_info=True)

    async def publish(self, event: Event) -> None:
        """Dispatch to all handlers, awaiting async ones."""
        self._audit(event)
        handlers = self._handlers_for(event.topic)
        for handler in handlers:
            await self._invoke(handler, event)

    def publish_nowait(self, event: Event) -> None:
        """Fire-and-forget dispatch; async handlers scheduled on the loop."""
        self._audit(event)
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
