"""Signal providers.

- Internal strategies (via the strategy engine)
- TradingView webhook alerts (HTTP endpoint with shared-secret auth)
"""

from __future__ import annotations

import asyncio
import hmac
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

from mercury.core.events import Event, EventBus
from mercury.core.logging import get_logger
from mercury.models.schemas import Direction, Signal, SignalSource

logger = get_logger("services.signal.providers")


class TradingViewWebhookServer:
    """Runs a FastAPI/uvicorn endpoint receiving TradingView alert POSTs.

    Alerts are authenticated via a shared secret (header ``X-Mercury-Secret``
    or JSON field ``secret``). Each alert is published as ``signal.received``.
    """

    def __init__(self, *, host: str, port: int, secret: str, bus: EventBus, mode: str = "paper") -> None:
        self.host = host
        self.port = port
        self.secret = secret
        self.mode = mode
        self.bus = bus
        self._server: Any = None
        self._task: asyncio.Task | None = None
        self.app = self._build_app()

    def _build_app(self) -> Any:
        app = FastAPI(title="Mercury Signal Webhook", docs_url=None, redoc_url=None)

        @app.post("/webhook")
        async def webhook(request: Request, x_mercury_secret: str | None = Header(default=None)) -> dict:
            if self.secret:
                body = await request.json() if request.headers.get("content-type") == "application/json" else {}
                provided = str(x_mercury_secret or (body or {}).get("secret") or "")
                if not hmac.compare_digest(provided, self.secret):
                    raise HTTPException(status_code=401, detail="invalid secret")
            else:
                body = await request.json()
            try:
                signal = self._parse_alert(body)
            except Exception as exc:  # noqa: BLE001
                logger.warning("invalid webhook payload", extra={"error": str(exc)})
                raise HTTPException(status_code=422, detail="invalid payload") from exc
            self.bus.publish_nowait(Event("signal.received", signal))
            return {"status": "received"}

        return app

    @staticmethod
    def _parse_alert(body: dict[str, Any]) -> Signal:
        """Parse a TradingView alert JSON body into a Signal.

        Expected fields: direction (long/short), optional symbol, price,
        stop_loss, take_profit. Extra keys tolerated (secret ignored).
        """
        direction_raw = str(body.get("direction") or body.get("side") or "").lower()
        if direction_raw not in ("long", "short", "buy", "sell"):
            raise ValueError(f"unknown direction: {direction_raw!r}")
        direction = Direction.LONG if direction_raw in ("long", "buy") else Direction.SHORT
        return Signal(
            provider=SignalSource.TRADINGVIEW,
            strategy_id=body.get("strategy_id"),
            symbol=str(body.get("symbol", "XAUUSD")),
            timeframe=str(body.get("timeframe", "M5")),
            direction=direction,
            price=float(body["price"]) if body.get("price") is not None else None,
            sl=float(body["sl"]) if body.get("sl") is not None else (float(body["stop_loss"]) if body.get("stop_loss") is not None else None),
            tp=float(body["tp"]) if body.get("tp") is not None else (float(body["take_profit"]) if body.get("take_profit") is not None else None),
            meta={k: v for k, v in body.items() if k not in ("secret", "sl", "tp", "stop_loss", "take_profit")},
            created_at=datetime.now(UTC),
        )

    async def start(self) -> None:
        import uvicorn

        if self.mode == "live" and not self.secret:
            raise RuntimeError(
                "refusing to start TradingView webhook in live mode without a secret — "
                "set SIGNAL_WEBHOOK_SECRET / providers.signal.webhook.secret"
            )
        if not self.secret:
            logger.warning(
                "TradingView webhook running WITHOUT authentication — only safe for "
                "local/paper use; set SIGNAL_WEBHOOK_SECRET before going live"
            )

        config = uvicorn.Config(self.app, host=self.host, port=self.port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        logger.info("webhook server listening", extra={"port": self.port})

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=3)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
