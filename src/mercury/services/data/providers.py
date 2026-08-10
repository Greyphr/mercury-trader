"""Market data providers.

- ``MT5MarketDataProvider``: reads from a running MetaTrader 5 terminal
  (Exness). Lazy-imported so the package remains importable without MT5.
- ``PaperMarketDataProvider``: local simulation for dev/offline runs.
"""

from __future__ import annotations

import math
import random
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any

from mercury.core.logging import get_logger
from mercury.core.validation import Candle

logger = get_logger("services.data.providers")

MT5_TIMEFRAMES = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 16385,
    "H4": 16388,
    "D1": 16408,
    "W1": 32769,
}

_MT5 = None  # module cache


def _mt5_module():
    """Lazily import the MetaTrader5 package. Returns None when unavailable."""
    global _MT5
    if _MT5 is not None:
        return _MT5
    try:
        import MetaTrader5 as mt5  # type: ignore

        _MT5 = mt5
    except ImportError:
        logger.warning("MetaTrader5 package not installed; MT5 providers unavailable")
        _MT5 = False
    return _MT5 or None


class MarketDataProvider(ABC):
    """Abstraction over live market data access."""

    @abstractmethod
    def connect(self) -> bool:
        ...

    @abstractmethod
    def disconnect(self) -> None:
        ...

    @abstractmethod
    def is_connected(self) -> bool:
        ...

    @abstractmethod
    def get_quote(self, symbol: str) -> dict[str, Any] | None:
        ...

    @abstractmethod
    def get_candles(self, symbol: str, timeframe: str, count: int) -> list[Candle]:
        ...

    def available_symbols(self) -> list[str]:
        """Broker symbols currently available (empty when unknown/offline)."""
        return []


class MT5MarketDataProvider(MarketDataProvider):
    """Reads market data from the MT5 terminal (same Windows machine)."""

    def __init__(self, *, login: str, password: str, server: str,
                 terminal_path: str = "", enable_launch: bool = True) -> None:
        self.login = login
        self.password = password
        self.server = server
        self.terminal_path = terminal_path
        self.enable_launch = enable_launch
        self._connected = False

    def connect(self) -> bool:
        mt5 = _mt5_module()
        if mt5 is None:
            logger.error("MetaTrader5 unavailable — cannot connect")
            return False
        kwargs: dict[str, Any] = {"login": int(self.login), "password": self.password, "server": self.server}
        if self.terminal_path:
            kwargs["path"] = self.terminal_path
        if not self.enable_launch:
            kwargs["portable"] = True
        if mt5.initialize(**kwargs):
            self._connected = True
            logger.info("connected to MT5", extra={"server": self.server})
            return True
        logger.error("MT5 initialize failed", extra={"error": mt5.last_error()})
        return False

    def disconnect(self) -> None:
        mt5 = _mt5_module()
        if mt5 is not None:
            mt5.shutdown()
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def get_quote(self, symbol: str) -> dict[str, Any] | None:
        mt5 = _mt5_module()
        if mt5 is None or not self._connected:
            return None
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return None
        spread = float(tick.ask - tick.bid)
        return {
            "symbol": symbol,
            "bid": float(tick.bid),
            "ask": float(tick.ask),
            "spread_points": spread,
            "time": datetime.fromtimestamp(tick.time, tz=timezone.utc),
        }

    def get_candles(self, symbol: str, timeframe: str, count: int) -> list[Candle]:
        mt5 = _mt5_module()
        if mt5 is None or not self._connected:
            return []
        tf = MT5_TIMEFRAMES.get(timeframe, 5)
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if rates is None or len(rates) == 0:
            return []
        rows = []
        for r in rates:
            rows.append(
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "time": datetime.fromtimestamp(r["time"], tz=timezone.utc),
                    "open": float(r["open"]),
                    "high": float(r["high"]),
                    "low": float(r["low"]),
                    "close": float(r["close"]),
                    "volume": float(r["tick_volume"]),
                }
            )
        return _validate(rows, symbol, timeframe)

    def available_symbols(self) -> list[str]:
        mt5 = _mt5_module()
        if mt5 is None or not self._connected:
            return []
        return [s.name for s in (mt5.symbols_get() or [])]


class PaperMarketDataProvider(MarketDataProvider):
    """Local simulation provider: generates a plausible random walk.

    Useful for development, tests, and demonstrating the pipeline without a
    broker connection. Production should use :class:`MT5MarketDataProvider`.
    """

    def __init__(self, *, seed: int | None = None, base_price: float = 2350.0, tick_volatility: float = 0.15) -> None:
        self._seed = seed
        self._rng = random.Random(seed)
        self.base_price = base_price
        self.tick_volatility = tick_volatility
        self._last: dict[str, dict[str, float]] = {}
        self._connected = False

    def connect(self) -> bool:
        self._connected = True
        return True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def get_quote(self, symbol: str) -> dict[str, Any] | None:
        if not self._connected:
            return None
        last = self._last.get(symbol)
        base = last["close"] if last else self.base_price
        drift = self._rng.uniform(-self.tick_volatility, self.tick_volatility)
        mid = max(1.0, base + drift)
        spread = 0.3
        return {
            "symbol": symbol,
            "bid": mid - spread / 2,
            "ask": mid + spread / 2,
            "spread_points": spread,
            "time": datetime.now(timezone.utc),
        }

    def get_candles(self, symbol: str, timeframe: str, count: int) -> list[Candle]:
        rows = []
        last = self._last.get(symbol)
        price = last["close"] if last else self.base_price
        now = datetime.now(timezone.utc)
        for i in range(count):
            t = now - timedelta(minutes=5 * (count - i))
            o = price
            c = max(1.0, o + self._rng.uniform(-self.tick_volatility, self.tick_volatility))
            h = max(o, c) + self._rng.uniform(0, self.tick_volatility)
            l = min(o, c) - self._rng.uniform(0, self.tick_volatility)
            price = c
            rows.append(
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "time": t,
                    "open": round(o, 2),
                    "high": round(h, 2),
                    "low": round(l, 2),
                    "close": round(c, 2),
                    "volume": float(self._rng.randint(100, 5000)),
                }
            )
        self._last[symbol] = {"close": price}
        return _validate(rows, symbol, timeframe)


def _validate(rows: list[dict[str, Any]], symbol: str, timeframe: str) -> list[Candle]:
    from mercury.core.validation import validate_candles

    return validate_candles(rows)


def make_data_provider(settings) -> MarketDataProvider:
    """Factory: build a provider based on config + the active environment."""
    cfg = settings.providers.data
    if cfg.backend == "mt5":
        creds = settings.environment.mt5.credentials()
        if not creds["login"] or not creds["password"]:
            logger.warning("MT5 credentials missing — falling back to paper provider")
            return PaperMarketDataProvider()
        return MT5MarketDataProvider(
            login=creds["login"],
            password=creds["password"],
            server=creds["server"],
            terminal_path=creds["terminal_path"],
            enable_launch=creds["enable_launch"],
        )
    return PaperMarketDataProvider()
