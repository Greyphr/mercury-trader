"""Tests for the Telegram notifier send queue (rate limit + 429 backoff)."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from mercury.services.notifications import providers
from mercury.services.notifications.providers import Notification, TelegramNotifier


class _FakeResponse:
    def __init__(self, status_code=200, *, body=None, headers=None) -> None:
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no json body")
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "request failed",
                request=httpx.Request("POST", "http://telegram.test"),
                response=self,
            )


class _FakeAsyncClient:
    """Records POST timestamps; replays a scripted response list."""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = list(responses)
        self.post_times: list[float] = []

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def post(self, *args, **kwargs) -> _FakeResponse:
        self.post_times.append(time.monotonic())
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


def _monkeypatch_http(monkeypatch, fake: _FakeAsyncClient) -> None:
    monkeypatch.setattr(providers.httpx, "AsyncClient", lambda **kw: fake)


@pytest.mark.asyncio
async def test_sends_respect_min_interval(monkeypatch):
    fake = _FakeAsyncClient([_FakeResponse()])
    _monkeypatch_http(monkeypatch, fake)

    notifier = TelegramNotifier(bot_token="t", chat_id="c", min_interval_seconds=0.15)
    notifier.start()
    try:
        results = await asyncio.gather(
            notifier.send(title="a", message="1"),
            notifier.send(title="b", message="2"),
            notifier.send(title="c", message="3"),
        )
        assert results == [True, True, True]
        assert len(fake.post_times) == 3
        gaps = [b - a for a, b in zip(fake.post_times[:-1], fake.post_times[1:], strict=True)]
        assert gaps, "expected multiple sends to be spaced"
        assert all(g >= 0.12 for g in gaps)
    finally:
        await notifier.close()


@pytest.mark.asyncio
async def test_429_with_retry_after_body_is_retried_not_dropped(monkeypatch):
    fake = _FakeAsyncClient(
        [
            _FakeResponse(429, body={"ok": False, "error_code": 429, "parameters": {"retry_after": 0.1}}),
            _FakeResponse(),
        ]
    )
    _monkeypatch_http(monkeypatch, fake)

    notifier = TelegramNotifier(bot_token="t", chat_id="c", min_interval_seconds=0.0)
    notifier.start()
    try:
        ok = await notifier.send(title="a", message="1")
        assert ok is True
        assert len(fake.post_times) == 2
        assert fake.post_times[1] - fake.post_times[0] >= 0.09
    finally:
        await notifier.close()


@pytest.mark.asyncio
async def test_429_retry_after_header_is_honored(monkeypatch):
    fake = _FakeAsyncClient([_FakeResponse(429, headers={"Retry-After": "0.1"}), _FakeResponse()])
    _monkeypatch_http(monkeypatch, fake)

    notifier = TelegramNotifier(bot_token="t", chat_id="c", min_interval_seconds=0.0)
    notifier.start()
    try:
        ok = await notifier.send(title="a", message="1")
        assert ok is True
        assert len(fake.post_times) == 2
        assert fake.post_times[1] - fake.post_times[0] >= 0.09
    finally:
        await notifier.close()


@pytest.mark.asyncio
async def test_429_gives_up_after_max_retries(monkeypatch):
    fake = _FakeAsyncClient([_FakeResponse(429, body={"parameters": {"retry_after": 0.01}})])
    _monkeypatch_http(monkeypatch, fake)

    notifier = TelegramNotifier(
        bot_token="t", chat_id="c", min_interval_seconds=0.0, max_retries=2
    )
    notifier.start()
    try:
        ok = await notifier.send(title="a", message="1")
        assert ok is False
        # retries consumed max_retries (2) POSTs, then gave up.
        assert len(fake.post_times) == 2
    finally:
        await notifier.close()


@pytest.mark.asyncio
async def test_queue_overflow_drops_oldest():
    notifier = TelegramNotifier(bot_token="t", chat_id="c", queue_size=2, min_interval_seconds=0.0)
    loop = asyncio.get_running_loop()
    notifier._queue = asyncio.Queue(maxsize=2)

    f1 = loop.create_future()
    f2 = loop.create_future()
    notifier._enqueue(Notification(title="a", message="1", level="info", future=f1))
    notifier._enqueue(Notification(title="b", message="2", level="info", future=f2))

    f3 = loop.create_future()
    notifier._enqueue(Notification(title="c", message="3", level="info", future=f3))

    assert f1.done() and f1.result() is False  # oldest dropped on overflow
    assert not f2.done()
    assert not f3.done()
