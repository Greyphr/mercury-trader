"""Historical data loading for backtests and analysis."""

from __future__ import annotations

from typing import Any

from mercury.core.symbols import get_symbol_mapper
from mercury.core.validation import Candle
from mercury.services.data.providers import MT5MarketDataProvider, make_data_provider


def load_history(settings, symbol: str, timeframe: str, count: int = 10000) -> list[Candle]:
    """Load recent candles from the configured provider (MT5 if available).

    ``symbol`` is the canonical instrument id (e.g. ``GOLD``); it is mapped to
    the broker symbol for the provider and the returned candles keep the
    canonical id. Falls back to the paper provider (synthetic) when MT5 is not
    configured.
    """
    broker_symbol = get_symbol_mapper(settings).broker_symbol(symbol)
    provider = make_data_provider(settings)
    try:
        if isinstance(provider, MT5MarketDataProvider) and not provider.is_connected():
            provider.connect()
        if not provider.is_connected():
            provider.connect()
        candles = provider.get_candles(broker_symbol, timeframe, count)
        return [c.model_copy(update={"symbol": symbol}) for c in candles]
    finally:
        if isinstance(provider, MT5MarketDataProvider):
            provider.disconnect()


def load_history_from_db(db, symbol: str, timeframe: str, count: int = 10000) -> list[dict[str, Any]]:
    """Load candles stored in PostgreSQL (fallback for offline backtests)."""
    from sqlalchemy import select

    from mercury.models.orm import CandleRecord

    with db.session() as session:
        rows = session.scalars(
            select(CandleRecord)
            .where(CandleRecord.symbol == symbol, CandleRecord.timeframe == timeframe)
            .order_by(CandleRecord.time.desc())
            .limit(count)
        ).all()
    return [
        {
            "symbol": r.symbol,
            "timeframe": r.timeframe,
            "time": r.time,
            "open": r.open,
            "high": r.high,
            "low": r.low,
            "close": r.close,
            "volume": r.volume,
        }
        for r in reversed(rows)
    ]
