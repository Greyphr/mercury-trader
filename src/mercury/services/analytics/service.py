"""Analytics service: periodic metrics snapshots for trend tracking."""

from __future__ import annotations

from typing import Any

from mercury.core.events import Event
from mercury.models.orm import MetricsRecord
from mercury.services.analytics.metrics import compute_metrics
from mercury.services.base import BackgroundService


class AnalyticsService(BackgroundService):
    """Periodically persists performance metrics snapshots."""

    name = "analytics"
    poll_interval_seconds: int = 3600

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.poll_interval_seconds = 3600

    async def tick(self) -> None:
        try:
            metrics = compute_metrics(self.db)
            with self.db.session() as session:
                session.add(MetricsRecord(period="periodic", metrics=metrics))
            await self.bus.publish(Event("analytics.snapshot", metrics))
            self.mark_healthy("metrics snapshot recorded")
        except Exception:  # noqa: BLE001
            self.logger.exception("analytics snapshot failed")
            self.mark_unhealthy("snapshot failed")
