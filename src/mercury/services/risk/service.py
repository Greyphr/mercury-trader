"""Risk management service.

Validates every candidate trade against the configured guards before it reaches
the execution engine. Enforces fixed-% position sizing, session/spread/news
filters, daily trade & drawdown limits, and a persisted kill switch.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from mercury.core.config import session_allows
from mercury.core.events import Event
from mercury.models.orm import SystemStateRecord, TradeRecord
from mercury.models.schemas import Signal, TradeStatus
from mercury.services.base import Service
from mercury.services.news.service import NewsService

EquityProvider = Callable[[], float]


@dataclass
class RiskDecision:
    approved: bool
    reasons: list[str] = field(default_factory=list)
    volume: float = 0.0
    risk_amount: float = 0.0

    @property
    def summary(self) -> str:
        return "approved" if self.approved else "; ".join(self.reasons)


class RiskManagerService(Service):
    """Enforces risk guards on every signal before execution."""

    name = "risk"

    def __init__(self, *, news_service: NewsService | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._news: NewsService | None = news_service
        self._equity_provider: EquityProvider | None = None
        self._last_quote: dict[str, Any] | None = None
        self._daily_pnl: float = 0.0
        self._daily_pnl_date: str = ""

    def set_equity_provider(self, provider: EquityProvider) -> None:
        self._equity_provider = provider

    async def start(self) -> None:
        await super().start()
        self.bus.subscribe("signal.assessed", self.on_signal_assessed)
        self.bus.subscribe("market.quote", self._on_quote)

    def _on_quote(self, event: Event) -> None:
        self._last_quote = event.payload

    # ── main entry ────────────────────────────────────────────
    async def on_signal_assessed(self, event: Event) -> None:
        payload = event.payload or {}
        signal: Signal = payload["signal"]
        signal_id = payload.get("signal_id")
        assessment = payload.get("assessment") or {}

        decision = self.evaluate(signal, assessment)

        if decision.approved:
            await self.bus.publish(
                Event(
                    "signal.approved",
                    {"signal": signal, "signal_id": signal_id, "risk": decision},
                )
            )
        else:
            self.logger.info("signal rejected", extra={"reasons": decision.reasons})
            await self.bus.publish(
                Event(
                    "signal.rejected",
                    {"signal": signal, "signal_id": signal_id, "reasons": decision.reasons},
                )
            )

    def evaluate(self, signal: Signal, assessment: dict[str, Any]) -> RiskDecision:
        """Run all guards. Returns approved/rejected decision with reasons."""
        guards = self.settings.risk.guards
        reasons: list[str] = []

        if not self.settings.base.deployment.can_trade:
            reasons.append("deployment mode does not allow trading")

        confidence = float(assessment.get("confidence", 0.0))
        if confidence < guards.min_confidence:
            reasons.append(f"confidence {confidence:.2f} < {guards.min_confidence}")

        if self._kill_switch_active():
            reasons.append("kill switch is active")

        if guards.session_check and not self._in_trading_session():
            reasons.append("outside configured trading session")

        if guards.max_open_positions and self._open_positions() >= guards.max_open_positions:
            reasons.append("max open positions reached")

        if guards.max_daily_trades and self._trades_today() >= guards.max_daily_trades:
            reasons.append("max daily trades reached")

        if guards.max_daily_drawdown_percent and self._today_pnl_percent() <= -guards.max_daily_drawdown_percent:
            reasons.append("max daily drawdown reached")

        quote_spread = (self._last_quote or {}).get("spread_points")
        if quote_spread is not None and quote_spread > guards.max_spread_points:
            reasons.append(f"spread {quote_spread} > {guards.max_spread_points}")

        if self._news is not None and self._news.is_in_blackout(guards.news_blackout_minutes):
            reasons.append("news blackout active")

        equity = self._equity()
        if equity is not None and equity < guards.min_account_equity:
            reasons.append(f"equity {equity:.2f} < {guards.min_account_equity}")

        if reasons:
            return RiskDecision(approved=False, reasons=reasons)

        volume, risk_amount = self._position_size(signal, equity)
        if volume <= 0:
            return RiskDecision(approved=False, reasons=["computed volume <= 0"])
        return RiskDecision(approved=True, volume=volume, risk_amount=risk_amount)

    # ── guards helpers ────────────────────────────────────────
    def _equity(self) -> float | None:
        if self._equity_provider is not None:
            try:
                return float(self._equity_provider())
            except Exception:  # noqa: BLE001
                self.logger.warning("equity provider failed")
                return None
        return None

    def _kill_switch_active(self) -> bool:
        cfg = self.settings.risk.kill_switch
        if not cfg.enabled:
            return False
        with self.db.session() as session:
            state = session.get(SystemStateRecord, "kill_switch")
        if state is None or not state.value.get("active"):
            return False
        armed_at = state.value.get("armed_at")
        try:
            if armed_at and datetime.fromisoformat(armed_at) + timedelta(hours=cfg.rearm_after_hours) < datetime.now(
                UTC
            ):
                self._set_kill_switch(False)
                return False
        except ValueError:
            pass
        return True

    def set_kill_switch(self, active: bool) -> None:
        self._set_kill_switch(active)

    def kill_switch_active(self) -> bool:
        """Public read of the persisted kill-switch state."""
        return self._kill_switch_active()

    def _set_kill_switch(self, active: bool) -> None:
        with self.db.session() as session:
            state = session.get(SystemStateRecord, "kill_switch")
            if state is None:
                state = SystemStateRecord(key="kill_switch", value={})
                session.add(state)
            state.value = {"active": active, "armed_at": datetime.now(UTC).isoformat()}

    def _in_trading_session(self) -> bool:
        return session_allows(self.settings.base.trading_sessions, datetime.now(UTC))

    def _open_positions(self) -> int:
        with self.db.session() as session:
            return int(
                session.scalar(
                    select(func.count()).select_from(TradeRecord).where(TradeRecord.status == TradeStatus.OPEN.value)
                )
                or 0
            )

    def _trades_today(self) -> int:
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        with self.db.session() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(TradeRecord)
                    .where(TradeRecord.opened_at >= start)
                )
                or 0
            )

    def _today_pnl_percent(self) -> float:
        """Return today's realized+unrealized P&L as % of starting balance (approx)."""
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        with self.db.session() as session:
            closed = session.scalars(
                select(TradeRecord).where(TradeRecord.status == TradeStatus.CLOSED.value)
            ).all()
        today = sum(t.pnl for t in closed if t.closed_at and t.closed_at >= start)
        equity = self._equity() or 0.0
        if equity <= 0:
            return 0.0
        return (today / equity) * 100.0

    def _position_size(self, signal: Signal, equity: float | None) -> tuple[float, float]:
        """Volume (lots) sized so SL distance risks the configured % of equity."""
        sizing = self.settings.risk.sizing
        risk_pct = self.settings.risk.risk_per_trade_percent / 100.0
        risk_pct = max(self.settings.risk.min_risk_per_trade_percent / 100.0,
                       min(self.settings.risk.max_risk_per_trade_percent / 100.0, risk_pct))

        entry = signal.price or 0.0
        sl = signal.sl
        if not entry or not sl:
            return 0.0, 0.0
        sl_distance = abs(entry - sl)
        if sl_distance <= 0:
            return 0.0, 0.0
        if equity is None:
            equity = 10_000.0
        contract = sizing.contract_size or 100.0
        volume = (equity * risk_pct) / (sl_distance * contract)
        volume = max(0.01, round(volume, 2))
        return volume, equity * risk_pct
