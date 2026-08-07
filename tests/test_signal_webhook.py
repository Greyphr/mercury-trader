import httpx
import pytest

from mercury.core.events import Event, EventBus
from mercury.services.signal.providers import TradingViewWebhookServer


def _make_bus():
    bus = EventBus()
    received: list[Event] = []
    bus.subscribe("signal.received", lambda e: received.append(e))
    return bus, received


def _server(*, secret: str, mode: str = "paper", bus: EventBus | None = None):
    return TradingViewWebhookServer(
        host="127.0.0.1", port=9100, secret=secret, bus=bus or EventBus(), mode=mode
    )


async def _post(server, *, headers=None, json=None):
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/webhook", headers=headers, json=json)


@pytest.mark.asyncio
async def test_correct_header_secret_succeeds():
    bus, received = _make_bus()
    server = _server(secret="s3cret", bus=bus)
    resp = await _post(server, headers={"X-Mercury-Secret": "s3cret"}, json={"direction": "long"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "received"
    assert len(received) == 1
    assert received[0].payload.provider.value == "tradingview"


@pytest.mark.asyncio
async def test_correct_body_secret_succeeds():
    bus, received = _make_bus()
    server = _server(secret="s3cret", bus=bus)
    resp = await _post(server, json={"direction": "long", "secret": "s3cret"})
    assert resp.status_code == 200
    assert len(received) == 1


@pytest.mark.asyncio
async def test_wrong_header_secret_rejected():
    bus, received = _make_bus()
    server = _server(secret="s3cret", bus=bus)
    resp = await _post(server, headers={"X-Mercury-Secret": "nope"}, json={"direction": "long"})
    assert resp.status_code == 401
    assert len(received) == 0


@pytest.mark.asyncio
async def test_wrong_body_secret_rejected():
    bus, received = _make_bus()
    server = _server(secret="s3cret", bus=bus)
    resp = await _post(server, json={"direction": "long", "secret": "nope"})
    assert resp.status_code == 401
    assert len(received) == 0


@pytest.mark.asyncio
async def test_missing_secret_rejected():
    bus, received = _make_bus()
    server = _server(secret="s3cret", bus=bus)
    resp = await _post(server, json={"direction": "long"})
    assert resp.status_code == 401
    assert len(received) == 0


@pytest.mark.asyncio
async def test_secret_still_required_outside_live_when_configured():
    server = _server(secret="s3cret", mode="paper")
    denied = await _post(server, json={"direction": "long"})
    assert denied.status_code == 401
    allowed = await _post(server, headers={"X-Mercury-Secret": "s3cret"}, json={"direction": "long"})
    assert allowed.status_code == 200


@pytest.mark.asyncio
async def test_refuses_to_start_in_live_without_secret():
    server = _server(secret="", mode="live")
    with pytest.raises(RuntimeError, match="live mode without a secret"):
        await server.start()


@pytest.mark.asyncio
async def test_allows_empty_secret_outside_live(monkeypatch):
    import uvicorn

    started = []

    class _FakeServer:
        should_exit = False

        def __init__(self, config) -> None:
            started.append(config)

        async def serve(self) -> None:
            pass

    monkeypatch.setattr(uvicorn, "Config", lambda *a, **k: "cfg")
    monkeypatch.setattr(uvicorn, "Server", _FakeServer)

    server = _server(secret="", mode="paper")
    await server.start()
    assert started
    await server.stop()
