"""Notification providers.

- Telegram (official Bot API). Additional providers (WhatsApp, email, Slack)
  implement :class:`Notifier` and register via the factory.
"""

from __future__ import annotations

import asyncio
import os
import time
from abc import ABC, abstractmethod
from asyncio import Future
from dataclasses import dataclass

import httpx

from mercury.core.logging import get_logger

logger = get_logger("services.notifications.providers")


class Notifier(ABC):
    """Abstract notification sink."""

    name: str = "notifier"

    @abstractmethod
    async def send(self, *, title: str, message: str, level: str = "info") -> bool:
        """Send a notification. Returns True on success."""
        ...

    @abstractmethod
    def start(self) -> None:
        """Optional lifecycle hook (e.g. start background workers)."""

    @abstractmethod
    async def close(self) -> None:
        """Optional lifecycle hook; release background resources."""


@dataclass(slots=True)
class Notification:
    """One queued send with a result future the caller awaits."""

    title: str
    message: str
    level: str
    future: Future[bool]
    attempts: int = 0


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: str,
        timeout: float = 20.0,
        min_interval_seconds: float = 1.1,
        queue_size: int = 100,
        max_retries: int = 3,
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout
        self.min_interval_seconds = min_interval_seconds
        self.queue_size = queue_size
        self.max_retries = max_retries
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._queue: asyncio.Queue[Notification] | None = None
        self._worker_task: asyncio.Task | None = None
        self._last_sent_at = 0.0
        self._closed = False

    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def start(self) -> None:
        """Start the background send worker (idempotent)."""
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._closed = False
        self._queue = asyncio.Queue(maxsize=self.queue_size)
        self._worker_task = asyncio.create_task(self._worker())

    async def close(self) -> None:
        """Stop the worker and fail any queued (undelivered) sends."""
        self._closed = True
        task, self._worker_task = self._worker_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        queue, self._queue = self._queue, None
        if queue is not None:
            while True:
                try:
                    item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if not item.future.done():
                    item.future.set_result(False)

    async def send(self, *, title: str, message: str, level: str = "info") -> bool:
        """Enqueue a send and await its delivery result.

        Actual HTTP sends are serialized by a single background worker that
        respects ``min_interval_seconds`` (Telegram ~1 msg/sec per chat) and
        retries after a 429 cooldown instead of dropping the message.
        """
        if not self.configured():
            logger.warning("telegram not configured — notification dropped")
            return False
        if self._closed:
            logger.warning("telegram notifier closed — notification dropped")
            return False
        if self._worker_task is None or self._worker_task.done():
            self.start()
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._enqueue(Notification(title=title, message=message, level=level, future=future))
        return await future

    # ── queue / worker ────────────────────────────────────────
    def _enqueue(self, item: Notification) -> None:
        if self._queue is None:
            item.future.set_result(False)
            return
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            try:
                dropped = self._queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - race only
                dropped = None
            if dropped is not None:
                logger.warning(
                    "telegram queue full — dropping oldest notification",
                    extra={"title": dropped.title},
                )
                dropped.future.set_result(False)
            self._queue.put_nowait(item)

    async def _worker(self) -> None:
        queue = self._queue
        while True:
            item = await queue.get()
            try:
                await self._throttle()
                retry_after = await self._deliver(item)
                if retry_after is not None:
                    item.attempts += 1
                    if item.attempts >= self.max_retries:
                        logger.error(
                            "telegram rate limited — giving up after retries",
                            extra={"title": item.title, "attempts": item.attempts},
                        )
                        item.future.set_result(False)
                    else:
                        logger.warning(
                            "telegram rate limited — retrying after cooldown",
                            extra={"retry_after": retry_after, "title": item.title},
                        )
                        await asyncio.sleep(retry_after)
                        self._enqueue(item)
            except asyncio.CancelledError:
                if not item.future.done():
                    item.future.set_result(False)
                raise
            except Exception:  # noqa: BLE001
                logger.exception("telegram send worker failed", extra={"title": item.title})
                if not item.future.done():
                    item.future.set_result(False)
            finally:
                queue.task_done()

    async def _throttle(self) -> None:
        """Space sends by ``min_interval_seconds`` measured from the last POST."""
        if self.min_interval_seconds <= 0:
            return
        elapsed = time.monotonic() - self._last_sent_at
        wait = self.min_interval_seconds - elapsed
        if wait > 0:
            await asyncio.sleep(wait)

    async def _deliver(self, item: Notification) -> float | None:
        """Attempt one send; return the retry-after cooldown on a 429."""
        icon = {"info": "ℹ️", "warn": "⚠️", "error": "❌", "critical": "🚨"}.get(item.level, "ℹ️")
        text = f"{icon} <b>{item.title}</b>\n{item.message}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    self._url,
                    json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
                )
        except httpx.HTTPError as exc:
            logger.error("telegram send failed", extra={"error": str(exc), "title": item.title})
            item.future.set_result(False)
            return None
        self._last_sent_at = time.monotonic()
        if resp.status_code == 429:
            return self._retry_after(resp)
        try:
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.error("telegram send failed", extra={"error": str(exc), "title": item.title})
            item.future.set_result(False)
            return None
        item.future.set_result(True)
        return None

    @staticmethod
    def _retry_after(resp) -> float:
        """Telegram body: ``parameters.retry_after``; fallback to the header."""
        try:
            parameters = (resp.json() or {}).get("parameters") or {}
            retry_after = parameters.get("retry_after")
            if isinstance(retry_after, (int, float)):
                return max(float(retry_after), 0.0)
        except ValueError:
            pass
        header = resp.headers.get("Retry-After")
        if header is not None:
            try:
                return max(float(header), 0.0)
            except ValueError:
                pass
        return 1.0


class ConsoleNotifier(Notifier):
    """Logs notifications to the console (dev fallback)."""

    name = "console"

    async def send(self, *, title: str, message: str, level: str = "info") -> bool:
        logger.info("NOTIFICATION [%s] %s: %s", level, title, message)
        return True

    def start(self) -> None:
        """No background worker needed."""

    async def close(self) -> None:
        """Nothing to release."""


def build_notifier(settings) -> Notifier:
    """Factory: build the configured notifier (env-driven)."""
    backend = settings.providers.notifications.backend
    if backend == "telegram":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = settings.providers.notifications.telegram.chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        notifier = TelegramNotifier(bot_token=token, chat_id=chat_id)
        if notifier.configured():
            return notifier
        logger.warning("telegram credentials incomplete — falling back to console")
    return ConsoleNotifier()
