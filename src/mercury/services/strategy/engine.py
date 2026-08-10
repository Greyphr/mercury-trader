"""Strategy engine: runs configured strategies on the latest closed candles
and emits signal candidates on the event bus."""

from __future__ import annotations

from typing import Any

from mercury.core.events import Event
from mercury.core.validation import Candle
from mercury.models.schemas import Signal
from mercury.services.base import Service
from mercury.services.strategy.ict import ICTStrategy
from mercury.services.strategy.strategies import Strategy, build_strategies
from mercury.services.strategy.trend_confluence import TrendConfluenceStrategy


class StrategyEngineService(Service):
    """Subscribes to market data updates and emits ``signal.received`` events."""

    name = "strategy.engine"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._strategies: list[Strategy] = build_strategies(
            self.settings.strategies.strategies, settings=self.settings
        )
        for strategy in self._strategies:
            if isinstance(strategy, (ICTStrategy, TrendConfluenceStrategy)):
                strategy.set_context_provider(self._context_provider)
        self._candles: dict[tuple[str, str], list[Candle]] = {}
        self._last_emitted: dict[tuple[str, str, str], str] = {}

    def _context_provider(self, symbol: str, timeframe: str, count: int) -> list[Candle]:
        from mercury.services.data.historical import load_history_from_db

        rows = load_history_from_db(self.db, symbol, timeframe, count=count)
        if not rows:
            return []
        return [Candle.model_validate(r) for r in rows]

    async def start(self) -> None:
        await super().start()
        self.bus.subscribe("market.data.updated", self._on_market_data)
        self.logger.info("strategies loaded", extra={"count": len(self._strategies)})

    def _on_market_data(self, event: Event) -> None:
        payload = event.payload or {}
        symbol = payload.get("symbol")
        timeframe = payload.get("timeframe")
        if not symbol or not timeframe:
            return
        self._run_for(symbol, timeframe)

    def _run_for(self, symbol: str, timeframe: str) -> None:
        from mercury.services.data.historical import load_history_from_db

        rows = load_history_from_db(self.db, symbol, timeframe, count=1000)
        if not rows:
            return
        parsed = [Candle.model_validate(r) for r in rows]
        self._candles[(symbol, timeframe)] = parsed

        closed = parsed[:-1] if len(parsed) > 1 else parsed
        if not closed:
            return
        # generate_signals scans the full history and returns every historical
        # match (required for backtesting); only the newest closed candle's
        # signals are live. Drop anything older so a stale setup can't trigger
        # a real trade.
        newest_closed_time = closed[-1].time.isoformat()
        for strategy in self._strategies:
            if strategy.config.symbol != symbol or strategy.config.timeframe != timeframe:
                continue
            for signal in strategy.generate_signals(closed):
                if signal.meta.get("candle_time") != newest_closed_time:
                    self.logger.debug(
                        "dropping stale signal — not from newest closed candle",
                        extra={
                            "strategy": signal.strategy_id,
                            "symbol": signal.symbol,
                            "direction": signal.direction.value,
                            "candle_time": signal.meta.get("candle_time"),
                            "newest_closed_time": newest_closed_time,
                        },
                    )
                    continue
                self._emit(signal)

    def _emit(self, signal: Signal) -> None:
        key = (signal.strategy_id or "", signal.symbol, signal.direction.value)
        candle_time = signal.meta.get("candle_time", "")
        if self._last_emitted.get(key) == candle_time:
            return
        self._last_emitted[key] = candle_time
        self.logger.info(
            "signal generated",
            extra={"strategy": signal.strategy_id, "symbol": signal.symbol,
                   "direction": signal.direction.value, "price": signal.price},
        )
        self.bus.publish_nowait(Event("signal.received", signal))
