"""Execution service: routes approved signals to the broker, persists trades,
and monitors open positions (TP/SL detection, MT5 server-side closes)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from mercury.core.events import Event
from mercury.core.symbols import SymbolMappingError, get_symbol_mapper
from mercury.models.orm import SystemStateRecord, TradeRecord
from mercury.models.schemas import Signal, TradeStatus
from mercury.services.base import BackgroundService
from mercury.services.execution.broker import (
    BrokerAdapter,
    ClosedTrade,
    MT5BrokerAdapter,
    PaperBrokerAdapter,
)


class ExecutionService(BackgroundService):
    """Executes approved signals and monitors positions."""

    name = "execution"
    poll_interval_seconds: int = 5

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._broker: BrokerAdapter | None = None
        self._prices: dict[str, dict[str, float]] = {}
        self._mapper = get_symbol_mapper(self.settings)
        self._trading_allowed = True
        self._stage_guard: Callable[[str], bool] | None = None
        self._reported_orphans: set[str] = set()
        self._startup_reconcile_issues: list[str] = []
        self._execution_lock = threading.Lock()
        self.poll_interval_seconds = self.settings.base.jobs.price_monitor

    @property
    def broker(self) -> BrokerAdapter | None:
        return self._broker

    @property
    def startup_reconcile_issues(self) -> list[str]:
        """Position-reconciliation problems found at startup (consumed by the
        startup validation gate to block trading)."""
        return list(self._startup_reconcile_issues)

    def set_trading_allowed(self, allowed: bool) -> None:
        """Arm/disarm order execution (called by the startup validation gate)."""
        self._trading_allowed = allowed
        self.logger.info("trading gate updated", extra={"allowed": allowed})

    def set_stage_guard(self, guard: Callable[[str], bool] | None) -> None:
        """Install the per-strategy lifecycle guard (wired by the orchestrator)."""
        self._stage_guard = guard

    async def start(self) -> None:
        await super().start()
        cfg = self.settings.providers.broker
        if not self.settings.environment.trading_enabled:
            self._broker = None
            self.logger.warning("execution disabled — environment not armed for trading")
            self.mark_healthy("execution disabled (trading not armed)")
            return
        if self.settings.base.deployment.mode == "read_only":
            self._broker = None
            self.logger.info("read_only mode — execution disabled")
            self.mark_healthy("execution disabled (read_only)")
            return
        if cfg.backend == "mt5":
            creds = self.settings.environment.mt5.credentials()
            if not creds["login"] or not creds["password"]:
                self.logger.warning("MT5 credentials missing — using paper broker")
                self._broker = PaperBrokerAdapter(contract_size=self._default_contract_size())
            else:
                self._broker = MT5BrokerAdapter(
                    login=creds["login"],
                    password=creds["password"],
                    server=creds["server"],
                    terminal_path=creds["terminal_path"],
                    slippage_points=cfg.mt5.slippage_points,
                    enable_launch=creds["enable_launch"],
                )
        else:
            self._broker = PaperBrokerAdapter(contract_size=self._default_contract_size())

        self.bus.subscribe("signal.approved", self._on_signal_approved)
        self.bus.subscribe("market.quote", self._on_quote)
        if self._broker.connect():
            self.mark_healthy(f"connected ({type(self._broker).__name__})")
            await self._reconcile_with_broker(record_for_gate=True)
        else:
            self.mark_unhealthy("broker connection failed")

    async def stop(self) -> None:
        if self._broker is not None:
            self._broker.disconnect()
        await super().stop()

    def _on_quote(self, event: Event) -> None:
        q = event.payload or {}
        self._prices[q.get("symbol")] = {"bid": q.get("bid", 0), "ask": q.get("ask", 0)}
        if isinstance(self._broker, PaperBrokerAdapter):
            self._broker.update_prices(self._prices)

    def _default_contract_size(self) -> float:
        for canonical in self._mapper.canonical_ids():
            try:
                return self._mapper.contract_size(canonical)
            except SymbolMappingError:
                continue
        return self.settings.risk.sizing.contract_size

    # ── order routing ─────────────────────────────────────────
    async def _on_signal_approved(self, event: Event) -> None:
        payload = event.payload or {}
        if not self._trading_allowed:
            self.logger.warning("signal ignored — trading gate closed")
            await self.bus.publish(
                Event("trade.rejected", {"error": "trading gate closed (startup validation failed)"})
            )
            return
        signal: Signal = payload["signal"]
        signal_id = payload.get("signal_id")
        if self._stage_guard is not None and not self._stage_guard(signal.strategy_id or ""):
            self.logger.warning(
                "signal ignored — strategy not approved for environment",
                extra={"strategy": signal.strategy_id},
            )
            await self.bus.publish(
                Event(
                    "trade.rejected",
                    {
                        "signal_id": signal_id,
                        "error": "strategy not approved for environment (lifecycle stage)",
                    },
                )
            )
            return
        if self._broker is None:
            return

        risk = payload.get("risk")
        volume = getattr(risk, "volume", 0.0)
        risk_amount = getattr(risk, "risk_amount", 0.0)
        magic = self._magic_for(signal)

        # MT5 speaks the broker symbol; the paper broker accounts in canonical ids.
        if isinstance(self._broker, MT5BrokerAdapter):
            try:
                order_symbol = self._mapper.broker_symbol(signal.symbol)
            except SymbolMappingError:
                self.logger.warning(
                    "signal symbol not in environment map — passing through",
                    extra={"symbol": signal.symbol},
                )
                order_symbol = signal.symbol
        else:
            order_symbol = signal.symbol

        max_open = self.settings.risk.guards.max_open_positions
        rejected_error: str | None = None
        trade_id: int | None = None
        ticket: str | None = None

        # The count-check, broker order, and TradeRecord insert form one
        # critical section: the in-process lock serializes them within this
        # process, and the FOR UPDATE guard row serializes them across
        # processes so concurrent signals can't exceed max_open_positions.
        with self._execution_lock:
            for _ in range(2):
                try:
                    with self.db.session() as session:
                        lock = session.get(SystemStateRecord, "open_positions_lock", with_for_update=True)
                        if lock is None:
                            session.add(SystemStateRecord(key="open_positions_lock", value={}))
                            session.flush()
                        if max_open and int(
                            session.scalar(
                                select(func.count())
                                .select_from(TradeRecord)
                                .where(TradeRecord.status == TradeStatus.OPEN.value)
                            )
                            or 0
                        ) >= max_open:
                            self.logger.warning(
                                "signal rejected — max open positions reached",
                                extra={"max": max_open},
                            )
                            rejected_error = f"max open positions reached ({max_open})"
                        else:
                            result = self._broker.open_market_order(
                                symbol=order_symbol,
                                direction=signal.direction.value,
                                volume=volume,
                                sl=signal.sl,
                                tp=signal.tp,
                                magic=magic,
                            )
                            if not result.success:
                                self.logger.error("order failed", extra={"error": result.error})
                                rejected_error = result.error
                            else:
                                record = TradeRecord(
                                    signal_id=signal_id,
                                    ticket=result.ticket,
                                    strategy_id=signal.strategy_id,
                                    symbol=signal.symbol,
                                    direction=signal.direction.value,
                                    volume=volume,
                                    entry_price=result.price or 0.0,
                                    sl=signal.sl,
                                    tp=signal.tp,
                                    status=TradeStatus.OPEN.value,
                                    risk_amount=risk_amount,
                                    deployment_mode=self.settings.base.deployment.mode,
                                    pre_trade_confidence=(payload.get("assessment") or {}).get("confidence"),
                                )
                                session.add(record)
                                session.flush()
                                trade_id = record.id
                                ticket = result.ticket
                    break
                except IntegrityError:
                    # Lost a concurrent guard-row insert (SQLite ignores FOR
                    # UPDATE); retry now that the row exists.
                    continue

        if rejected_error:
            await self.bus.publish(
                Event("trade.rejected", {"signal_id": signal_id, "error": rejected_error})
            )
            return

        self.logger.info(
            "trade opened",
            extra={"ticket": ticket, "trade_id": trade_id,
                   "direction": signal.direction.value, "volume": volume},
        )
        await self.bus.publish(
            Event(
                "trade.opened",
                {"trade_id": trade_id, "ticket": ticket, "signal": signal, "volume": volume},
            )
        )

    def _magic_for(self, signal: Signal) -> int:
        for strategy in self.settings.strategies.strategies:
            if strategy.id == signal.strategy_id:
                return strategy.order.magic
        return self.settings.providers.broker.mt5.magic

    def _open_positions_count(self) -> int:
        with self.db.session() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(TradeRecord)
                    .where(TradeRecord.status == TradeStatus.OPEN.value)
                )
                or 0
            )

    # ── monitoring ────────────────────────────────────────────
    async def tick(self) -> None:
        if self._broker is None:
            return
        try:
            await self._reconcile_with_broker()
            await self._reconcile_positions()
            await self.bus.publish(
                Event("account.updated", {"equity": self._broker.account_equity()})
            )
        except Exception:  # noqa: BLE001
            self.logger.exception("position monitor tick failed")

    async def _reconcile_with_broker(self, *, record_for_gate: bool = False) -> None:
        """Compare broker-open positions against OPEN TradeRecords.

        - Broker position with no matching record (orphan): alert via a single
          batched ``system.critical`` summary (not one event per ticket); when
          ``record_for_gate`` (startup pass) the issue is surfaced to the
          startup validation gate so trading is blocked.
        - OPEN record with no broker position: settle from the broker's trade
          history if the close is known, otherwise flag it ``MANUAL_REVIEW``
          (no PnL guessing).

        Per-ticket detail is always logged at ``critical`` level; only the
        fan-out to the event bus is collapsed into one summary event.
        """
        if self._broker is None:
            return
        broker_positions = self._broker.get_positions()
        broker_tickets = {p.ticket for p in broker_positions}
        with self.db.session() as session:
            open_records = session.query(TradeRecord).filter(
                TradeRecord.status == TradeStatus.OPEN.value
            ).all()
            db_tickets = {r.ticket for r in open_records if r.ticket}

        orphan_tickets = broker_tickets - db_tickets
        orphan_details = [
            detail
            for ticket in orphan_tickets
            if (detail := self._report_orphan(ticket, record_for_gate=record_for_gate))
        ]

        missing_details: list[str] = []
        missing_tickets = db_tickets - broker_tickets
        for record in open_records:
            if record.ticket and record.ticket in missing_tickets:
                detail = await self._resolve_missing_position(
                    record, record_for_gate=record_for_gate, publish=False
                )
                if detail:
                    missing_details.append(detail)

        # Drop reports for orphans that resolved, so a recurrence re-alerts.
        self._reported_orphans &= orphan_tickets

        if orphan_details or missing_details:
            self.bus.publish_nowait(
                Event(
                    "system.critical",
                    {"error": self._reconciliation_summary(orphan_details, missing_details)},
                )
            )

    @staticmethod
    def _reconciliation_summary(orphan_details: list[str], missing_details: list[str]) -> str:
        """Collapse per-item reconciliation alerts into one critical message."""
        counts = []
        if orphan_details:
            counts.append(f"{len(orphan_details)} orphaned broker position(s) with no TradeRecord")
        if missing_details:
            counts.append(f"{len(missing_details)} open record(s) with no broker match (manual review)")
        lines = [f"Reconciliation: {', '.join(counts)}."]
        lines.extend(orphan_details)
        lines.extend(missing_details)
        return "\n".join(lines)

    def _report_orphan(self, ticket: str, *, record_for_gate: bool) -> str | None:
        detail = (
            f"broker position {ticket} has no matching TradeRecord — "
            "reconcile or close it before trading"
        )
        alert = ticket not in self._reported_orphans
        if alert:
            self._reported_orphans.add(ticket)
            self.logger.critical("orphaned broker position", extra={"ticket": ticket})
        if record_for_gate and detail not in self._startup_reconcile_issues:
            self._startup_reconcile_issues.append(detail)
        return detail if alert else None

    async def _resolve_missing_position(
        self, record, *, record_for_gate: bool, publish: bool = True
    ) -> str | None:
        """An OPEN record whose ticket is gone from the broker.

        Flags the record ``MANUAL_REVIEW`` and returns the alert detail.
        ``publish`` controls whether a per-record ``system.critical`` event is
        emitted: ad hoc calls keep it, while the batched reconcile path
        suppresses it in favor of a single summary event.
        """
        closed = self._broker.closed_trades_since({record.ticket})
        if closed:
            await self._settle_trade(next(c for c in closed if c.ticket == record.ticket))
            return None

        detail = (
            f"open TradeRecord {record.ticket} ({record.symbol}) has no broker position "
            "and no close history — flagged for manual review"
        )
        with self.db.session() as session:
            current = session.get(TradeRecord, record.id)
            if current is None or current.status != TradeStatus.OPEN.value:
                return None
            current.status = TradeStatus.MANUAL_REVIEW.value
            current.meta = {
                **(current.meta or {}),
                "reconcile": "missing_broker_position",
                "manual_review": True,
            }
            session.flush()
        self.logger.critical("open trade missing at broker — manual review required",
                             extra={"ticket": record.ticket, "symbol": record.symbol})
        if publish:
            self.bus.publish_nowait(Event("system.critical", {"error": detail}))
        if record_for_gate and detail not in self._startup_reconcile_issues:
            self._startup_reconcile_issues.append(detail)
        return detail

    async def _reconcile_positions(self) -> None:
        with self.db.session() as session:
            open_records = session.query(TradeRecord).filter(
                TradeRecord.status == TradeStatus.OPEN.value
            ).all()
            open_tickets = {r.ticket for r in open_records if r.ticket}

        if not open_tickets:
            return

        # Paper broker: evaluate TP/SL against live quotes.
        if isinstance(self._broker, PaperBrokerAdapter):
            closed = self._broker.check_exits(self._prices)
            for trade in closed:
                await self._settle_trade(trade)

        # MT5: detect server-side closes.
        if isinstance(self._broker, MT5BrokerAdapter):
            closed_trades = self._broker.closed_trades_since(open_tickets)
            for trade in closed_trades:
                await self._settle_trade(trade)

        await self._manage_ict_positions()

    # ── ICT management (Spec V1) ──────────────────────────────
    async def _manage_ict_positions(self) -> None:
        """Apply the ICT management rules to open ICT-strategy positions:
        move SL to breakeven at +1R and exit early on an opposite M5 BOS."""
        if self._broker is None:
            return
        with self.db.session() as session:
            open_records = session.query(TradeRecord).filter(
                TradeRecord.status == TradeStatus.OPEN.value
            ).all()

        for record in open_records:
            cfg = self._strategy_cfg(record.strategy_id)
            if cfg is None or cfg.ict is None:
                continue
            ict = cfg.ict
            pos = self._broker_position(record.ticket)
            if pos is None:
                continue
            risk = abs(record.entry_price - (record.sl or record.entry_price))
            if risk <= 0:
                continue

            if ict.management.breakeven_at_r and not (record.meta or {}).get("be_triggered"):
                px = self._price_for(record.symbol)
                if px is not None:
                    pnl_r = (px - record.entry_price) / risk if record.direction == "long" \
                        else (record.entry_price - px) / risk
                    if pnl_r >= ict.management.breakeven_at_r:
                        result = self._broker.modify_position(record.ticket, sl=round(record.entry_price, 2))
                        if result.success:
                            with self.db.session() as session:
                                r = session.get(TradeRecord, record.id)
                                if r:
                                    r.sl = round(record.entry_price, 2)
                                    r.meta = {**(r.meta or {}), "be_triggered": True}
                            self.logger.info("breakeven moved", extra={"ticket": record.ticket})

            if ict.management.early_exit_on_opposite_bos:
                close_price = self._opposite_bos_close(record)
                if close_price is None:
                    continue
                if isinstance(self._broker, PaperBrokerAdapter):
                    trade = self._broker.close_position_trade(
                        record.ticket, reason="bos", price=close_price
                    )
                else:
                    result = self._broker.close_position(record.ticket)
                    trade = self._closed_trade(record, close_price, "bos") if result.success else None
                if trade is not None:
                    await self._settle_trade(trade)

    def _strategy_cfg(self, strategy_id: str | None):
        if strategy_id is None:
            return None
        for s in self.settings.strategies.strategies:
            if s.id == strategy_id:
                return s
        return None

    def _broker_position(self, ticket: str):
        if self._broker is None:
            return None
        for pos in self._broker.get_positions():
            if pos.ticket == ticket:
                return pos
        return None

    def _price_for(self, symbol: str) -> float | None:
        px = self._prices.get(symbol)
        if not px:
            return None
        bid, ask = px.get("bid", 0.0), px.get("ask", 0.0)
        if bid and ask:
            return (bid + ask) / 2.0
        return bid or ask or None

    def _opposite_bos_close(self, record) -> float | None:
        """Close price of the M5 candle that broke structure against the trade."""
        from mercury.core.validation import Candle
        from mercury.services.data.historical import load_history_from_db
        from mercury.services.strategy import indicators as ind

        rows = load_history_from_db(self.db, record.symbol, "M5", count=400)
        if not rows:
            return None
        candles = [Candle.model_validate(r) for r in rows]
        candles = [c for c in candles if c.time >= record.opened_at]
        if len(candles) < 10:
            return None
        idx = ind.detect_opposite_bos(candles, record.direction)
        if idx is None:
            return None
        return candles[idx].close

    def _closed_trade(self, record, price: float, reason: str) -> ClosedTrade:
        direction = 1 if record.direction == "long" else -1
        try:
            contract_size = self._mapper.contract_size(record.symbol)
        except SymbolMappingError:
            contract_size = self.settings.risk.sizing.contract_size
        pnl = direction * (price - record.entry_price) * contract_size * record.volume
        return ClosedTrade(
            ticket=record.ticket,
            symbol=record.symbol,
            direction=record.direction,
            volume=record.volume,
            entry=record.entry_price,
            close_price=round(price, 2),
            sl=record.sl,
            tp=record.tp,
            close_reason=reason,
            pnl=pnl,
            opened_at=record.opened_at,
            closed_at=datetime.now(UTC),
        )

    async def _settle_trade(self, trade: ClosedTrade) -> None:
        with self.db.session() as session:
            record = session.query(TradeRecord).filter(
                TradeRecord.ticket == trade.ticket, TradeRecord.status == TradeStatus.OPEN.value
            ).first()
            if record is None:
                return
            record.status = TradeStatus.CLOSED.value
            record.closed_at = trade.closed_at
            record.close_price = trade.close_price
            record.close_reason = trade.close_reason
            record.pnl = trade.pnl
            if record.risk_amount and record.risk_amount > 0:
                record.pnl_r = trade.pnl / record.risk_amount
            record.meta = {**(record.meta or {}), "closed_by": "monitor"}
            session.flush()
            trade_id = record.id

        self.logger.info(
            "trade closed",
            extra={"ticket": trade.ticket, "reason": trade.close_reason, "pnl": trade.pnl},
        )
        await self.bus.publish(
            Event("trade.closed", {"trade_id": trade_id, "ticket": trade.ticket, "trade": trade})
        )
