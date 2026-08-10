"""Continuous market data collection.

Polls the active data provider, upserts candles to the database, and emits
events so downstream services (strategy, Hermes, analytics) can react.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from mercury.core.events import Event
from mercury.core.symbols import SymbolMappingError, get_symbol_mapper
from mercury.core.validation import Candle
from mercury.models.orm import CandleRecord
from mercury.services.base import BackgroundService
from mercury.services.data.providers import MarketDataProvider, make_data_provider


class DataCollectorService(BackgroundService):
    """Collects quotes and candles for configured canonical symbols/timeframes."""

    name = "data.collector"
    poll_interval_seconds: int = 60

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        cfg = self.settings.providers.data
        self._provider: MarketDataProvider = make_data_provider(self.settings)
        self._mapper = get_symbol_mapper(self.settings)
        # (canonical, broker_symbol, timeframe) — broker symbols are what the
        # provider speaks; canonical ids are what the DB/strategies use.
        self._symbols: list[tuple[str, str, str]] = []
        for strategy in self.settings.strategies.strategies:
            if not strategy.enabled:
                continue
            try:
                broker = self._mapper.broker_symbol(strategy.symbol)
            except SymbolMappingError as exc:
                self.logger.warning(
                    "skipping strategy with unmapped symbol",
                    extra={"strategy": strategy.id, "error": str(exc)},
                )
                continue
            self._symbols.append((strategy.symbol, broker, strategy.timeframe))
            if strategy.ict is not None:
                self._symbols.append((strategy.symbol, broker, "H1"))
                self._symbols.append((strategy.symbol, broker, "H4"))
        if not self._symbols:
            try:
                broker = self._mapper.broker_symbol("GOLD")
            except SymbolMappingError:
                broker = "GOLD"
            self._symbols = [("GOLD", broker, "M5")]
        seen: set[tuple[str, str]] = set()
        self._symbols = [
            (c, b, tf) for c, b, tf in self._symbols if not ((c, tf) in seen or seen.add((c, tf)))
        ]
        self.poll_interval_seconds = self.settings.base.jobs.market_data

    @property
    def provider(self) -> MarketDataProvider:
        return self._provider

    async def start(self) -> None:
        await super().start()
        if not self._provider.connect():
            self.mark_unhealthy("data provider connection failed")
            return
        self._verify_symbols()
        self.mark_healthy(f"connected ({type(self._provider).__name__})")

    async def stop(self) -> None:
        self._provider.disconnect()
        await super().stop()

    def _verify_symbols(self) -> None:
        """Cross-check the environment symbol map against the broker's symbols.

        Stage 1: logs warnings for missing/ambiguous symbols. The startup
        validation gate (Stage 2) will turn failures into blocking checks.
        """
        try:
            available = self._provider.available_symbols()
        except Exception:  # noqa: BLE001
            self.logger.debug("symbol discovery unavailable", exc_info=True)
            return
        if not available:
            return
        verified = self._mapper.verify_available(available)
        self.logger.info("broker symbol map verified", extra={"canonical": sorted(verified)})

    async def tick(self) -> None:
        if not self._provider.is_connected():
            self.mark_unhealthy("provider disconnected")
            return
        for canonical, broker_symbol, timeframe in self._symbols:
            try:
                quote = self._provider.get_quote(broker_symbol)
                if quote:
                    await self.bus.publish(Event("market.quote", {**quote, "symbol": canonical}))
                candles = self._provider.get_candles(broker_symbol, timeframe, 300)
                if candles:
                    candles = [c.model_copy(update={"symbol": canonical}) for c in candles]
                    self._store_candles(candles)
                    await self.bus.publish(
                        Event("market.data.updated", {"symbol": canonical, "timeframe": timeframe, "candles": len(candles)})
                    )
            except Exception:  # noqa: BLE001
                self.logger.exception("collection tick failed", extra={"symbol": canonical})
        self.mark_healthy("tick ok")

    def _store_candles(self, candles: list[Candle]) -> None:
        with self.db.session() as session:
            existing = {
                (c.symbol, c.timeframe, c.time) for c in session.scalars(
                    select(CandleRecord).where(
                        CandleRecord.symbol == candles[0].symbol,
                        CandleRecord.timeframe == candles[0].timeframe,
                        CandleRecord.time.in_([c.time for c in candles]),
                    )
                )
            }
            to_insert = [
                CandleRecord(
                    symbol=c.symbol,
                    timeframe=c.timeframe,
                    time=c.time,
                    open=c.open,
                    high=c.high,
                    low=c.low,
                    close=c.close,
                    volume=c.volume,
                )
                for c in candles
                if (c.symbol, c.timeframe, c.time) not in existing
            ]
            if to_insert:
                session.add_all(to_insert)
