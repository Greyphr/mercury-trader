"""News collection service: pulls configured sources, stores events, and
exposes blackout windows used by the risk manager before entries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from mercury.core.events import Event
from mercury.models.orm import NewsEventRecord
from mercury.services.base import BackgroundService
from mercury.services.news.providers import NewsProvider, build_providers


class NewsService(BackgroundService):
    """Collects news/sentiment events from configured providers."""

    name = "news.collector"
    poll_interval_seconds: int = 300

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._providers: list[NewsProvider] = build_providers(self.settings)
        self.poll_interval_seconds = self.settings.base.jobs.news_collection
        self._recent: list[NewsEventRecord] = []

    async def tick(self) -> None:
        if not self._providers:
            return
        collected: list[NewsEventRecord] = []
        with httpx.Client() as client:
            for provider in self._providers:
                try:
                    for item in provider.fetch(client):
                        rec = NewsEventRecord(
                            source=item.source,
                            title=item.title,
                            url=item.url,
                            impact=item.impact,
                            currency=item.currency,
                            event_time=item.event_time,
                            sentiment_score=item.sentiment_score,
                            raw=item.raw,
                        )
                        collected.append(rec)
                except Exception:  # noqa: BLE001
                    self.logger.exception("news provider failed", extra={"provider": provider.name})

        with self.db.session() as session:
            session.add_all(collected)
        self._recent = collected
        if collected:
            await self.bus.publish(Event("news.collected", {"events": len(collected)}))
            self.mark_healthy(f"{len(collected)} events")

    def is_in_blackout(self, minutes: int = 5, at: datetime | None = None) -> bool:
        """True when ``at`` falls within ``minutes`` before or after any
        collected high-impact economic event (no new entries allowed)."""
        if not self._recent:
            return False
        now = at or datetime.now(UTC)
        window = timedelta(minutes=minutes)
        for rec in self._recent:
            if not rec.event_time:
                continue
            if rec.impact not in ("high", "red", "H"):
                continue
            if abs(now - rec.event_time) <= window:
                return True
        return False
