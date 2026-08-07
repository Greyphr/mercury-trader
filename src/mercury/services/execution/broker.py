"""Broker adapters.

- ``MT5BrokerAdapter``: executes via the MetaTrader 5 terminal (Exness).
- ``PaperBrokerAdapter``: local simulation with TP/SL evaluation, used for
  paper trading and development.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from mercury.core.logging import get_logger

logger = get_logger("services.execution.broker")


@dataclass
class OrderResult:
    success: bool
    ticket: str | None = None
    price: float | None = None
    error: str = ""


@dataclass
class Position:
    ticket: str
    symbol: str
    direction: str
    volume: float
    entry: float
    sl: float | None = None
    tp: float | None = None
    opened_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class ClosedTrade:
    ticket: str
    symbol: str
    direction: str
    volume: float
    entry: float
    close_price: float
    sl: float | None
    tp: float | None
    close_reason: str
    pnl: float
    opened_at: datetime
    closed_at: datetime


class BrokerAdapter(ABC):
    """Abstract broker interface (swappable: MT5, paper, others)."""

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
    def account_equity(self) -> float | None:
        ...

    @abstractmethod
    def open_market_order(
        self,
        *,
        symbol: str,
        direction: str,
        volume: float,
        sl: float | None,
        tp: float | None,
        magic: int = 0,
    ) -> OrderResult:
        ...

    @abstractmethod
    def get_positions(self, symbol: str | None = None) -> list[Position]:
        ...

    @abstractmethod
    def close_position(self, ticket: str) -> OrderResult:
        ...

    @abstractmethod
    def modify_position(self, ticket: str, *, sl: float | None = None, tp: float | None = None) -> OrderResult:
        ...

    @abstractmethod
    def closed_trades_since(self, tickets: set[str]) -> list[ClosedTrade]:
        """Return ClosedTrade records for known tickets no longer open."""
        ...


class MT5BrokerAdapter(BrokerAdapter):
    def __init__(self, *, login: str, password: str, server: str,
                 terminal_path: str = "", slippage_points: int = 20,
                 enable_launch: bool = True) -> None:
        self.login = login
        self.password = password
        self.server = server
        self.terminal_path = terminal_path
        self.slippage_points = slippage_points
        self.enable_launch = enable_launch
        self._connected = False
        self._mt5 = None

    def connect(self) -> bool:
        try:
            import MetaTrader5 as mt5  # type: ignore

            self._mt5 = mt5
        except ImportError:
            logger.error("MetaTrader5 package not installed")
            return False
        kwargs: dict[str, Any] = {"login": int(self.login), "password": self.password, "server": self.server}
        if self.terminal_path:
            kwargs["path"] = self.terminal_path
        if not self.enable_launch:
            kwargs["portable"] = True
        if self._mt5.initialize(**kwargs):
            self._connected = True
            logger.info("MT5 broker connected")
            return True
        logger.error("MT5 initialize failed", extra={"error": self._mt5.last_error()})
        return False

    def disconnect(self) -> None:
        if self._mt5 is not None:
            self._mt5.shutdown()
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def account_equity(self) -> float:
        if self._mt5 is None or not self._connected:
            return 0.0
        info = self._mt5.account_info()
        return float(info.equity) if info else 0.0

    def open_market_order(self, *, symbol: str, direction: str, volume: float,
                          sl: float | None, tp: float | None, magic: int = 0) -> OrderResult:
        if self._mt5 is None or not self._connected:
            return OrderResult(success=False, error="not connected")
        from MetaTrader5 import ORDER_TYPE_BUY, ORDER_TYPE_SELL  # type: ignore

        order_type = ORDER_TYPE_BUY if direction == "long" else ORDER_TYPE_SELL
        request = {
            "action": 1,  # TRADE_ACTION_DEAL
            "symbol": symbol,
            "volume": float(volume),
            "type": order_type,
            "price": self._mt5.symbol_info_tick(symbol).ask if direction == "long"
            else self._mt5.symbol_info_tick(symbol).bid,
            "sl": sl,
            "tp": tp,
            "deviation": self.slippage_points,
            "magic": magic,
            "comment": "mercury",
            "type_time": 0,
            "type_filling": self._mt5.ORDER_FILLING_IOC,
        }
        result = self._mt5.order_send(request)
        if result is None:
            return OrderResult(success=False, error=str(self._mt5.last_error()))
        if result.retcode != 10009:  # TRADE_RETCODE_DONE
            return OrderResult(success=False, error=f"retcode {result.retcode}")
        return OrderResult(success=True, ticket=str(result.order), price=float(result.price))

    def get_positions(self, symbol: str | None = None) -> list[Position]:
        if self._mt5 is None or not self._connected:
            return []
        positions = self._mt5.positions_get(symbol=symbol)
        out: list[Position] = []
        for p in positions or []:
            out.append(
                Position(
                    ticket=str(p.ticket),
                    symbol=p.symbol,
                    direction="long" if p.type == 0 else "short",
                    volume=float(p.volume),
                    entry=float(p.price_open),
                    sl=float(p.sl) if p.sl else None,
                    tp=float(p.tp) if p.tp else None,
                    opened_at=datetime.fromtimestamp(p.time, tz=UTC),
                )
            )
        return out

    def close_position(self, ticket: str) -> OrderResult:
        if self._mt5 is None or not self._connected:
            return OrderResult(success=False, error="not connected")
        position = self._mt5.positions_get(ticket=int(ticket))
        if not position:
            return OrderResult(success=False, error="position not found")
        pos = position[0]
        from MetaTrader5 import ORDER_TYPE_BUY, ORDER_TYPE_SELL  # type: ignore

        close_type = ORDER_TYPE_BUY if pos.type == 1 else ORDER_TYPE_SELL
        tick = self._mt5.symbol_info_tick(pos.symbol)
        request = {
            "action": 1,
            "symbol": pos.symbol,
            "volume": float(pos.volume),
            "type": close_type,
            "position": pos.ticket,
            "price": tick.ask if close_type == ORDER_TYPE_BUY else tick.bid,
            "deviation": self.slippage_points,
            "magic": int(pos.magic),
            "comment": "mercury close",
            "type_time": 0,
            "type_filling": self._mt5.ORDER_FILLING_IOC,
        }
        result = self._mt5.order_send(request)
        if result is None:
            return OrderResult(success=False, error=str(self._mt5.last_error()))
        if result.retcode != 10009:
            return OrderResult(success=False, error=f"retcode {result.retcode}")
        return OrderResult(success=True, ticket=str(result.order), price=float(result.price))

    def modify_position(self, ticket: str, *, sl: float | None = None, tp: float | None = None) -> OrderResult:
        if self._mt5 is None or not self._connected:
            return OrderResult(success=False, error="not connected")
        position = self._mt5.positions_get(ticket=int(ticket))
        if not position:
            return OrderResult(success=False, error="position not found")
        pos = position[0]
        request = {
            "action": 4,  # TRADE_ACTION_SLTP
            "symbol": pos.symbol,
            "position": pos.ticket,
            "sl": sl if sl is not None else (float(pos.sl) if pos.sl else 0.0),
            "tp": tp if tp is not None else (float(pos.tp) if pos.tp else 0.0),
        }
        result = self._mt5.order_send(request)
        if result is None:
            return OrderResult(success=False, error=str(self._mt5.last_error()))
        if result.retcode != 10009:
            return OrderResult(success=False, error=f"retcode {result.retcode}")
        return OrderResult(success=True, ticket=ticket, price=float(pos.price_open))

    def closed_trades_since(self, tickets: set[str]) -> list[ClosedTrade]:
        if self._mt5 is None or not self._connected or not tickets:
            return []
        out: list[ClosedTrade] = []
        from datetime import datetime as dt

        for tstr in tickets:
            ticket = int(tstr)
            deals = self._mt5.history_deals_get(ticket=ticket)
            if not deals:
                continue
            # Position tickets appear as deal "position" — match by position id.
            for deal in deals:
                if deal.position != ticket:
                    continue
                profit = float(deal.profit)
                price = float(deal.price)
                reason = _mt5_deal_reason(deal.reason)
                position = self._mt5.positions_get(ticket=ticket)
                if position:
                    pos = position[0]
                    entry = float(pos.price_open)
                    vol = float(pos.volume)
                    opened = dt.fromtimestamp(pos.time, tz=UTC)
                else:
                    orders = self._mt5.history_orders_get(ticket=ticket)
                    o = orders[0] if orders else None
                    entry = float(o.price_open) if o is not None else price
                    vol = float(o.volume_initial) if o is not None else 0.0
                    opened = dt.fromtimestamp(o.time_setup, tz=UTC) if o is not None else dt.now(UTC)
                out.append(
                    ClosedTrade(
                        ticket=tstr,
                        symbol=deal.symbol,
                        direction="long" if deal.type == 0 else "short",
                        volume=vol,
                        entry=entry,
                        close_price=price,
                        sl=float(deal.sl) if deal.sl else None,
                        tp=float(deal.tp) if deal.tp else None,
                        close_reason=reason,
                        pnl=profit,
                        opened_at=opened,
                        closed_at=dt.fromtimestamp(deal.time, tz=UTC),
                    )
                )
        return out


class PaperBrokerAdapter(BrokerAdapter):
    """In-memory simulated broker with TP/SL evaluation."""

    name = "paper"

    def __init__(self, *, starting_balance: float = 10_000.0, contract_size: float = 100.0) -> None:
        self.starting_balance = starting_balance
        self.contract_size = contract_size
        self.balance = starting_balance
        self.positions: dict[str, Position] = {}
        self.closed: list[ClosedTrade] = []
        self._prices: dict[str, dict[str, float]] = {}
        self._connected = False

    def connect(self) -> bool:
        self._connected = True
        return True

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def account_equity(self) -> float | None:
        unrealized = 0.0
        for pos in self.positions.values():
            u = self._unrealized(pos)
            if u is None:
                return None
            unrealized += u
        return self.balance + unrealized

    def update_prices(self, prices: dict[str, dict[str, float]]) -> None:
        """Cache the latest bid/ask quotes (same shape as ``check_exits`` price_map)."""
        self._prices = dict(prices)

    def open_market_order(self, *, symbol: str, direction: str, volume: float,
                          sl: float | None, tp: float | None, magic: int = 0) -> OrderResult:
        if not self._connected:
            return OrderResult(success=False, error="not connected")
        entry = self._last_price(symbol)
        if entry is None:
            return OrderResult(success=False, error=f"no quote available for symbol '{symbol}'")
        ticket = uuid.uuid4().hex[:12]
        self.positions[ticket] = Position(
            ticket=ticket,
            symbol=symbol,
            direction=direction,
            volume=volume,
            entry=round(entry, 2),
            sl=sl,
            tp=tp,
        )
        return OrderResult(success=True, ticket=ticket, price=self.positions[ticket].entry)

    def get_positions(self, symbol: str | None = None) -> list[Position]:
        if symbol:
            return [p for p in self.positions.values() if p.symbol == symbol]
        return list(self.positions.values())

    def close_position(self, ticket: str) -> OrderResult:
        trade = self.close_position_trade(ticket)
        if trade is None:
            return OrderResult(success=False, error="position not found or no quote available")
        return OrderResult(success=True, ticket=ticket, price=trade.close_price)

    def close_position_trade(
        self, ticket: str, *, reason: str = "manual", price: float | None = None
    ) -> ClosedTrade | None:
        """Close a position and return the settled trade (paper accounting)."""
        pos = self.positions.get(ticket)
        if pos is None:
            return None
        close_price = price if price is not None else self._last_price(pos.symbol)
        if close_price is None:
            return None
        self.positions.pop(ticket, None)
        return self._settle(pos, close_price, reason)

    def modify_position(self, ticket: str, *, sl: float | None = None, tp: float | None = None) -> OrderResult:
        pos = self.positions.get(ticket)
        if pos is None:
            return OrderResult(success=False, error="position not found")
        if sl is not None:
            pos.sl = sl
        if tp is not None:
            pos.tp = tp
        return OrderResult(success=True, ticket=ticket, price=pos.entry)

    def closed_trades_since(self, tickets: set[str]) -> list[ClosedTrade]:
        return [c for c in self.closed if c.ticket in tickets]

    def check_exits(self, price_map: dict[str, dict[str, float]]) -> list[ClosedTrade]:
        """Evaluate TP/SL against current prices (call from monitor)."""
        settled: list[ClosedTrade] = []
        for ticket, pos in list(self.positions.items()):
            px = price_map.get(pos.symbol)
            if not px:
                continue
            if pos.direction == "long":
                if pos.tp and px.get("ask", 0) >= pos.tp:
                    self.positions.pop(ticket)
                    settled.append(self._settle(pos, pos.tp, "tp"))
                elif pos.sl and px.get("bid", 0) <= pos.sl:
                    self.positions.pop(ticket)
                    settled.append(self._settle(pos, pos.sl, "sl"))
            else:
                if pos.tp and px.get("bid", 0) <= pos.tp:
                    self.positions.pop(ticket)
                    settled.append(self._settle(pos, pos.tp, "tp"))
                elif pos.sl and px.get("ask", 0) >= pos.sl:
                    self.positions.pop(ticket)
                    settled.append(self._settle(pos, pos.sl, "sl"))
        return settled

    # ── helpers ───────────────────────────────────────────────
    def _settle(self, pos: Position, exit_price: float, reason: str) -> ClosedTrade:
        direction = 1 if pos.direction == "long" else -1
        pnl = direction * (exit_price - pos.entry) * self.contract_size * pos.volume
        self.balance += pnl
        trade = ClosedTrade(
            ticket=pos.ticket,
            symbol=pos.symbol,
            direction=pos.direction,
            volume=pos.volume,
            entry=pos.entry,
            close_price=round(exit_price, 2),
            sl=pos.sl,
            tp=pos.tp,
            close_reason=reason,
            pnl=pnl,
            opened_at=pos.opened_at,
            closed_at=datetime.now(UTC),
        )
        self.closed.append(trade)
        return trade

    def _unrealized(self, pos: Position) -> float | None:
        price = self._last_price(pos.symbol)
        if price is None:
            return None
        direction = 1 if pos.direction == "long" else -1
        return direction * (price - pos.entry) * self.contract_size * pos.volume

    def _last_price(self, symbol: str) -> float | None:
        px = self._prices.get(symbol)
        if not px:
            return None
        bid, ask = px.get("bid", 0.0), px.get("ask", 0.0)
        if bid and ask:
            return (bid + ask) / 2.0
        return bid or ask or None


def _mt5_deal_reason(reason: int) -> str:
    """Map MT5 deal reason codes to CloseReason values."""
    try:
        from MetaTrader5 import (  # type: ignore
            DEAL_REASON_CLIENT,
            DEAL_REASON_EXPIRATION,
            DEAL_REASON_SL,
            DEAL_REASON_TP,
        )
    except ImportError:  # pragma: no cover
        return "unknown"
    if reason == DEAL_REASON_TP:
        return "tp"
    if reason == DEAL_REASON_SL:
        return "sl"
    if reason == DEAL_REASON_CLIENT:
        return "manual"
    if reason == DEAL_REASON_EXPIRATION:
        return "timeout"
    return "unknown"
