"""Signal service: validates and persists incoming signals from any provider,
then emits ``signal.validated`` for Hermes assessment."""

from __future__ import annotations

from typing import Any

from mercury.core.events import Event
from mercury.models.orm import SignalRecord
from mercury.models.schemas import Signal
from mercury.services.base import Service
from mercury.services.signal.providers import TradingViewWebhookServer


class SignalService(Service):
    """Central entry point for all signal sources."""

    name = "signal"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._webhook: TradingViewWebhookServer | None = None

    async def start(self) -> None:
        await super().start()
        self.bus.subscribe("signal.received", self._on_signal)
        if "tradingview" in self.settings.providers.signal.providers:
            cfg = self.settings.providers.signal.webhook
            self._webhook = TradingViewWebhookServer(
                host=cfg.host, port=cfg.port, secret=cfg.secret, bus=self.bus,
                mode=self.settings.deployment_mode,
            )
            await self._webhook.start()
        self.mark_healthy("signal service ready")

    async def stop(self) -> None:
        if self._webhook is not None:
            await self._webhook.stop()
        await super().stop()

    async def _on_signal(self, event: Event) -> None:
        payload = event.payload
        if not isinstance(payload, Signal):
            try:
                payload = Signal.model_validate(payload)
            except Exception:  # noqa: BLE001
                self.logger.error("unrecognized signal payload")
                return

        signal: Signal = payload
        record = SignalRecord(
            provider=signal.provider.value,
            strategy_id=signal.strategy_id,
            symbol=signal.symbol,
            timeframe=signal.timeframe,
            direction=signal.direction.value,
            price=signal.price,
            sl=signal.sl,
            tp=signal.tp,
            meta=signal.meta,
        )
        with self.db.session() as session:
            session.add(record)
            session.flush()
            signal_id = record.id

        self.logger.info(
            "signal validated",
            extra={"signal_id": signal_id, "provider": signal.provider.value,
                   "direction": signal.direction.value},
        )
        await self.bus.publish(
            Event("signal.validated", {"signal": signal, "signal_id": signal_id})
        )
