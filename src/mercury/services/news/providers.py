"""News & sentiment providers.

Free-first adapters, each with graceful degradation:
- RSS feeds (XML parsing via stdlib)
- Economic calendar (Forex Factory best-effort HTML parse)
- Fear & Greed sentiment index (CNN endpoint)
"""

from __future__ import annotations

import html as _html
import re
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from mercury.core.logging import get_logger

_FF_TZ = ZoneInfo("America/New_York")
_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

logger = get_logger("services.news.providers")


def _node_text(item, tag: str) -> str | None:
    node = item.find(tag)
    return node.text.strip() if node is not None and node.text else None


@dataclass
class NewsItem:
    source: str
    title: str
    url: str | None = None
    impact: str | None = None
    currency: str | None = None
    event_time: datetime | None = None
    sentiment_score: float | None = None
    raw: dict = field(default_factory=dict)


class NewsProvider(ABC):
    name: str = "news"

    @abstractmethod
    def fetch(self, client: httpx.Client) -> list[NewsItem]:
        ...


class RSSNewsProvider(NewsProvider):
    """Parse standard RSS 2.0 feeds."""

    name = "rss"

    def __init__(self, feeds: list[str]) -> None:
        self.feeds = feeds

    def fetch(self, client: httpx.Client) -> list[NewsItem]:
        items: list[NewsItem] = []
        for url in self.feeds:
            try:
                resp = client.get(url, timeout=15)
                resp.raise_for_status()
                items.extend(self._parse(resp.text, url))
            except Exception:  # noqa: BLE001
                logger.warning("RSS fetch failed", extra={"url": url})
        return items

    @staticmethod
    def _parse(xml_text: str, feed_url: str) -> list[NewsItem]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            logger.warning("RSS parse error", extra={"url": feed_url})
            return []
        items: list[NewsItem] = []
        for item in root.iter("item"):
            title = _node_text(item, "title")
            if not title:
                continue
            items.append(
                NewsItem(
                    source="rss",
                    title=title,
                    url=_node_text(item, "link"),
                    event_time=datetime.now(UTC),
                )
            )
        return items


def _ff_cell(row: str, cls: str) -> str | None:
    """Extract the text of a Forex Factory table cell by CSS class."""
    m = re.search(r'<td[^>]*class="[^"]*' + re.escape(cls) + r'[^"]*"[^>]*>(.*?)</td>', row, re.S)
    if not m:
        return None
    text = re.sub(r"<[^>]+>", "", m.group(1))
    return _html.unescape(text).strip()


def _ff_impact(row: str) -> str | None:
    m = re.search(r"impact--(high|medium|low|holiday)", row)
    return m.group(1) if m else None


def _ff_shift_to_weekday(day: date, name: str) -> date:
    key = name.strip().lower()[:3]
    if key not in _WEEKDAYS:
        return day
    delta = (_WEEKDAYS[key] - day.weekday()) % 7
    return day + timedelta(days=delta)


def _ff_time(text: str | None, day: date) -> datetime | None:
    """Parse a Forex Factory clock time (e.g. '8:30am', '12:00pm') shown in
    Eastern Time into an aware UTC datetime. Untimed rows (All Day / Tentative)
    are dropped so we never store a NULL event time."""
    if not text:
        return None
    s = text.strip().lower()
    if not s or s in ("all day", "all-day", "tentative"):
        return None
    for fmt in ("%I:%M%p", "%I%p", "%H:%M"):
        try:
            parsed = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    else:
        return None
    naive = datetime.combine(day, parsed.time())
    return naive.replace(tzinfo=_FF_TZ).astimezone(UTC)


class EconomicCalendarProvider(NewsProvider):
    """Best-effort economic calendar (Forex Factory).

    No official free API exists; this parses the public calendar page
    (``?day=today``) and degrades gracefully to an empty result on any
    change/failure. Page times default to Eastern Time and are converted to
    UTC. Only high-impact events with a parseable time are returned by default
    (``major_impact_only``), which covers NFP / CPI / FOMC.
    """

    name = "economic_calendar"

    def __init__(self, *, source: str = "forex_factory", major_impact_only: bool = True,
                 blackout_minutes: int = 15) -> None:
        self.source = source
        self.major_impact_only = major_impact_only
        self.blackout_minutes = blackout_minutes
        self._url = "https://www.forexfactory.com/calendar?day=today"

    def fetch(self, client: httpx.Client) -> list[NewsItem]:
        if self.source != "forex_factory":
            logger.warning("unsupported economic calendar source", extra={"source": self.source})
            return []
        try:
            resp = client.get(self._url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            items = self._parse(resp.text)
        except Exception:  # noqa: BLE001
            logger.warning("forex factory fetch failed", exc_info=True)
            return []
        if items:
            logger.info("forex factory events parsed", extra={"count": len(items)})
        return items

    def _parse(self, html_text: str) -> list[NewsItem]:
        rows = re.split(r'<tr class="calendar_row', html_text)[1:]
        today = datetime.now(UTC).astimezone(_FF_TZ).date()
        items: list[NewsItem] = []
        for row in rows:
            impact = _ff_impact(row)
            if impact is None:
                continue
            if self.major_impact_only and impact != "high":
                continue
            event = _ff_cell(row, "calendar__event")
            if not event:
                continue
            day = today
            day_name = _ff_cell(row, "calendar__day")
            if day_name:
                day = _ff_shift_to_weekday(today, day_name)
            event_time = _ff_time(_ff_cell(row, "calendar__time"), day)
            if event_time is None:
                continue
            items.append(
                NewsItem(
                    source="forex_factory",
                    title=event,
                    impact=impact,
                    currency=_ff_cell(row, "calendar__currency"),
                    event_time=event_time,
                    raw={"impact": impact},
                )
            )
        return items


class FearGreedSentimentProvider(NewsProvider):
    """CNN Fear & Greed index as a market-sentiment signal."""

    name = "fear_greed"

    def __init__(self, url: str | None = None) -> None:
        self.url = url or "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"

    def fetch(self, client: httpx.Client) -> list[NewsItem]:
        try:
            resp = client.get(self.url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            data = resp.json()
            score = data.get("fear_and_greed_score", {}).get("score")
            if score is None:
                return []
            return [
                NewsItem(
                    source="fear_greed",
                    title=f"Fear & Greed index: {score}/100",
                    event_time=datetime.now(UTC),
                    sentiment_score=float(score),
                )
            ]
        except Exception:  # noqa: BLE001
            logger.warning("fear & greed fetch failed")
            return []


def build_providers(settings) -> list[NewsProvider]:
    """Build enabled news providers from config."""
    cfg = settings.providers.news
    providers: list[NewsProvider] = []
    backends = cfg.backends or []
    if "rss" in backends:
        providers.append(RSSNewsProvider(cfg.rss.feeds))
    if "economic_calendar" in backends:
        providers.append(EconomicCalendarProvider(**cfg.economic_calendar.model_dump()))
    if "fear_greed" in backends:
        providers.append(FearGreedSentimentProvider(cfg.sentiment.fear_greed_url))
    return providers
