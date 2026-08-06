"""Base class for all services.

A service owns a slice of the system, subscribes to relevant events, exposes
a lifecycle (``start``/``stop``) and a ``health`` status. Services are wired
together by the orchestrator and communicate via the event bus.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from mercury.core.config import Settings
from mercury.core.db import Database
from mercury.core.events import EventBus
from mercury.core.logging import get_logger


class Service(ABC):
    """Abstract base service."""

    name: str = "service"

    def __init__(self, *, bus: EventBus, settings: Settings, db: Database, **_: Any) -> None:
        self.bus = bus
        self.settings = settings
        self.db = db
        self.logger = get_logger(f"services.{self.name}")
        self._running = False
        self._health_ok = True
        self._health_detail: str = "initialized"

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def health(self) -> tuple[bool, str]:
        return self._health_ok, self._health_detail

    @abstractmethod
    async def start(self) -> None:
        """Start the service (called once by the orchestrator)."""
        self._running = True
        self._health_ok = True
        self._health_detail = "running"
        self.logger.info("service started")

    async def stop(self) -> None:
        """Gracefully stop the service."""
        self._running = False
        self.logger.info("service stopped")

    def mark_healthy(self, detail: str = "ok") -> None:
        self._health_ok = True
        self._health_detail = detail

    def mark_unhealthy(self, detail: str) -> None:
        self._health_ok = False
        self._health_detail = detail
        self.logger.error("service unhealthy", extra={"detail": detail})


class BackgroundService(Service):
    """A service that runs a periodic task loop (polling cadence)."""

    poll_interval_seconds: int = 60

    async def start(self) -> None:
        await super().start()

    @abstractmethod
    async def tick(self) -> None:
        """One work cycle. Runs every ``poll_interval_seconds``."""
        raise NotImplementedError
