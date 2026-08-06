"""Notification providers.

- Telegram (official Bot API). Additional providers (WhatsApp, email, Slack)
  implement :class:`Notifier` and register via the factory.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod

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


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(self, *, bot_token: str, chat_id: str, timeout: float = 20.0) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    async def send(self, *, title: str, message: str, level: str = "info") -> bool:
        if not self.configured():
            logger.warning("telegram not configured — notification dropped")
            return False
        icon = {"info": "ℹ️", "warn": "⚠️", "error": "❌", "critical": "🚨"}.get(level, "ℹ️")
        text = f"{icon} <b>{title}</b>\n{message}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self._url, json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"})
                resp.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            logger.error("telegram send failed", extra={"error": str(exc)})
            return False


class ConsoleNotifier(Notifier):
    """Logs notifications to the console (dev fallback)."""

    name = "console"

    async def send(self, *, title: str, message: str, level: str = "info") -> bool:
        logger.info("NOTIFICATION [%s] %s: %s", level, title, message)
        return True


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
