"""Notification service: subscribes to system events, formats messages, and
sends via the active notifier. Also builds daily/weekly/monthly reports."""

from __future__ import annotations

from typing import Any

from mercury.core.events import Event
from mercury.services.analytics.metrics import compute_metrics_snapshot
from mercury.services.base import Service
from mercury.services.notifications.providers import Notifier, build_notifier


class NotificationService(Service):
    name = "notifications"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._notifier: Notifier = build_notifier(self.settings)

    async def start(self) -> None:
        await super().start()
        self.bus.subscribe("trade.opened", self._on_trade_opened)
        self.bus.subscribe("trade.closed", self._on_trade_closed)
        self.bus.subscribe("trade.rejected", self._on_trade_rejected)
        self.bus.subscribe("signal.rejected", self._on_signal_rejected)
        self.bus.subscribe("hermes.proposal.backtested", self._on_proposal_backtested)
        self.bus.subscribe("strategy.promoted", self._on_strategy_promoted)
        self.bus.subscribe("system.critical", self._on_critical)
        self.mark_healthy(f"notifier: {self._notifier.name}")

    # ── event handlers ────────────────────────────────────────
    async def _on_trade_opened(self, event: Event) -> None:
        p = event.payload or {}
        signal = p.get("signal")
        direction = signal.direction.value if signal else "?"
        await self._notifier.send(
            title="Trade Opened",
            message=(
                f"<b>{direction.upper()}</b> {signal.symbol if signal else 'XAUUSD'} "
                f"@ {signal.price if signal else '-'}\n"
                f"SL: {signal.sl if signal else '-'} | TP: {signal.tp if signal else '-'}\n"
                f"Volume: {p.get('volume')}"
            ),
            level="info",
        )

    async def _on_trade_closed(self, event: Event) -> None:
        p = event.payload or {}
        trade = p.get("trade")
        if trade is None:
            return
        outcome = "✅ TP" if trade.close_reason == "tp" else ("🔻 SL" if trade.close_reason == "sl" else "ℹ️ close")
        await self._notifier.send(
            title=f"Trade Closed — {outcome}",
            message=(
                f"{trade.direction.upper()} {trade.symbol} | PnL: <b>{trade.pnl:+.2f} USDT</b> "
                f"(R: {trade.pnl_r:+.2f})\n"
                f"Entry {trade.entry} → Exit {trade.close_price} | Reason: {trade.close_reason}"
            ),
            level="info",
        )

    async def _on_trade_rejected(self, event: Event) -> None:
        p = event.payload or {}
        await self._notifier.send(
            title="Order Rejected", message=f"{p.get('error', 'unknown error')}", level="warn"
        )

    async def _on_signal_rejected(self, event: Event) -> None:
        p = event.payload or {}
        reasons = p.get("reasons") or []
        await self._notifier.send(
            title="Signal Rejected", message="\n".join(f"• {r}" for r in reasons), level="warn"
        )

    async def _on_proposal_backtested(self, event: Event) -> None:
        p = event.payload or {}
        passed = p.get("passed")
        title = "Hermes Proposal: Backtest Passed" if passed else "Hermes Proposal: Backtest Failed"
        level = "info" if passed else "warn"
        summary = (p.get("summary") or {}).get("metrics", {})
        message = (
            f"Proposal #{p.get('proposal_id')}\n"
            f"Trades: {summary.get('trades')} | Win rate: {summary.get('win_rate')} "
            f"| Profit factor: {summary.get('profit_factor')} "
            f"| Expectancy R: {summary.get('expectancy_r')}"
        )
        await self._notifier.send(title=title, message=message, level=level)

    async def _on_strategy_promoted(self, event: Event) -> None:
        p = event.payload or {}
        await self._notifier.send(
            title="Strategy Promoted",
            message=(
                f"<b>{p.get('strategy_id')}</b>: {p.get('from_stage')} → <b>{p.get('to_stage')}</b>\n"
                f"Actor: {p.get('actor')}\n{p.get('reason') or ''}"
            ),
            level="info",
        )

    async def _on_critical(self, event: Event) -> None:
        p = event.payload or {}
        await self._notifier.send(
            title="Critical System Error", message=p.get("error", "unknown"), level="critical"
        )

    # ── reports ───────────────────────────────────────────────
    async def send_daily_report(self) -> bool:
        metrics = compute_metrics_snapshot(self.db, period="daily")
        message = self._format_report("Daily Report", metrics)
        return await self._notifier.send(title="📊 Daily Report", message=message, level="info")

    async def send_weekly_report(self) -> bool:
        metrics = compute_metrics_snapshot(self.db, period="weekly")
        return await self._notifier.send(
            title="📈 Weekly Report", message=self._format_report("Weekly", metrics), level="info"
        )

    async def send_monthly_report(self) -> bool:
        metrics = compute_metrics_snapshot(self.db, period="monthly")
        return await self._notifier.send(
            title="📉 Monthly Report", message=self._format_report("Monthly", metrics), level="info"
        )

    @staticmethod
    def _format_report(period: str, metrics: dict[str, Any]) -> str:
        lines = [
            f"<b>{period}</b>",
            f"Trades: {metrics.get('total_trades')}",
            f"Win rate: <b>{metrics.get('win_rate', 0) * 100:.1f}%</b>",
            f"Profit factor: {metrics.get('profit_factor')}",
            f"Expectancy (R): {metrics.get('expectancy_r')}",
            f"Net PnL: {metrics.get('total_pnl'):+.2f}",
            f"Max drawdown: {metrics.get('max_drawdown_percent')}%",
            f"Sharpe: {metrics.get('sharpe_ratio')}",
            f"Consecutive losses: {metrics.get('consecutive_losses')}",
        ]
        return "\n".join(lines)

    async def send_hermes_insight(self, summary: str) -> bool:
        return await self._notifier.send(title="🤖 Hermes Insight", message=summary, level="info")

    async def send_startup_validation(self, results, *, passed: bool) -> bool:
        """Report the startup validation checklist results (immediate alert)."""
        lines = [f"{'✅' if r.ok else '❌'} {r.name}: {r.detail}" for r in results if r.relevant]
        title = "Startup Validation — Trading Enabled" if passed else "Startup Validation — TRADING BLOCKED"
        return await self._notifier.send(
            title=title, message="\n".join(lines) or "no relevant checks", level="info" if passed else "critical"
        )
