"""Learning engine.

Owns the self-improvement loop:
1. Records every trade, signal, and rejection (via the trade ledger).
2. Computes win-vs-loss comparisons for Hermes.
3. Receives Hermes proposals and validates them through backtesting.
4. Tracks proposal lifecycle: proposed → backtested → human approval →
   paper → live (never auto-promoted without a human gate).
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

from sqlalchemy import select

from mercury.core.events import Event
from mercury.models.orm import ProposalRecord, StrategyVersionRecord, TradeRecord
from mercury.models.schemas import ProposalStatus
from mercury.services.base import Service


class LearningService(Service):
    name = "learning"

    async def start(self) -> None:
        await super().start()
        self.bus.subscribe("hermes.proposals", self._on_proposals)
        self.bus.subscribe("trade.closed", self._on_trade_closed)

    # ── ledger side-effects ───────────────────────────────────
    async def _on_trade_closed(self, event: Event) -> None:
        # Learning-relevant computations happen on demand; nothing to persist
        # here beyond what execution already stored.
        self.logger.debug("trade closed recorded", extra={"trade_id": event.payload.get("trade_id")})

    def win_loss_comparison(self, *, limit: int = 200) -> dict[str, Any]:
        """Compare winning vs losing trades (used by Hermes and reports)."""
        with self.db.session() as session:
            trades = session.scalars(
                select(TradeRecord).where(TradeRecord.status == "closed").order_by(TradeRecord.closed_at.desc()).limit(limit)
            ).all()
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl < 0]

        def avg(field_name: str, group: list[TradeRecord]) -> float | None:
            vals = [getattr(t, field_name) for t in group if getattr(t, field_name) is not None]
            return round(sum(vals) / len(vals), 4) if vals else None

        def avg_hold(group: list[TradeRecord]) -> float | None:
            holds = [
                (t.closed_at - t.opened_at).total_seconds() / 60
                for t in group
                if t.opened_at and t.closed_at
            ]
            return round(sum(holds) / len(holds), 2) if holds else None

        return {
            "total": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_avg_pnl_r": avg("pnl_r", wins),
            "loss_avg_pnl_r": avg("pnl_r", losses),
            "win_avg_hold_minutes": avg_hold(wins),
            "loss_avg_hold_minutes": avg_hold(losses),
            "win_avg_confidence": avg("pre_trade_confidence", wins),
            "loss_avg_confidence": avg("pre_trade_confidence", losses),
            "loss_reasons": self._reason_counts(losses),
            "win_reasons": self._reason_counts(wins),
        }

    @staticmethod
    def _reason_counts(group: list[TradeRecord]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for t in group:
            key = t.close_reason or "unknown"
            counts[key] = counts.get(key, 0) + 1
        return counts

    # ── improvement pipeline ─────────────────────────────────
    async def _on_proposals(self, event: Event) -> None:
        ids = (event.payload or {}).get("proposal_ids") or []
        for pid in ids:
            await self._backtest_proposal(pid)

    async def _backtest_proposal(self, proposal_id: int) -> None:
        with self.db.session() as session:
            proposal = session.get(ProposalRecord, proposal_id)
            if proposal is None:
                return
            proposal.status = ProposalStatus.BACKTESTING.value
            cfg_merge = proposal.proposed_config or {}
            strategy_cfg = self._merge_strategy_config(cfg_merge)
            symbol = strategy_cfg.symbol
            timeframe = strategy_cfg.timeframe

        from mercury.core.validation import Candle
        from mercury.services.backtest.engine import build_strategy_for_backtest, run_backtest
        from mercury.services.data.historical import load_history

        candles_raw = load_history(self.settings, symbol, timeframe, count=10_000)
        candles = [Candle.model_validate(c) if isinstance(c, dict) else c for c in candles_raw]
        if len(candles) < 100:
            with self.db.session() as session:
                proposal = session.get(ProposalRecord, proposal_id)
                if proposal:
                    proposal.status = ProposalStatus.REJECTED.value
                    proposal.review_notes = "insufficient historical data for backtest"
            return

        try:
            strategy = build_strategy_for_backtest(strategy_cfg, self.settings)
            result = run_backtest(
                strategy,
                candles,
                risk_percent=self.settings.risk.risk_per_trade_percent,
                contract_size=self.settings.risk.sizing.contract_size,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("backtest failed", extra={"proposal_id": proposal_id})
            with self.db.session() as session:
                proposal = session.get(ProposalRecord, proposal_id)
                if proposal:
                    proposal.status = ProposalStatus.REJECTED.value
                    proposal.review_notes = f"backtest error: {exc}"
            return

        summary = result.to_result()
        passed = self._passes_paper_gate(summary)
        with self.db.session() as session:
            proposal = session.get(ProposalRecord, proposal_id)
            if proposal is None:
                return
            proposal.backtest_result = summary
            proposal.status = (
                ProposalStatus.AWAITING_HUMAN.value if passed else ProposalStatus.REJECTED.value
            )
            proposal.review_notes = (
                "backtest passed; awaiting human approval for paper trading"
                if passed
                else "backtest failed promotion gates"
            )
        await self.bus.publish(
            Event("hermes.proposal.backtested", {"proposal_id": proposal_id, "passed": passed, "summary": summary})
        )

    def _merge_strategy_config(self, overrides: dict[str, Any]) -> Any:
        """Start from the current strategy config and apply proposal overrides."""
        base = self.settings.strategies.strategies[0]
        data = base.model_dump()
        deep_merge(data, overrides)
        from mercury.core.config import StrategyConfig

        return StrategyConfig.model_validate(data)

    def approve_proposal(self, proposal_id: int, *, stage: str = "paper") -> bool:
        """Human approval gate. ``stage`` is 'paper' or 'live'."""
        with self.db.session() as session:
            proposal = session.get(ProposalRecord, proposal_id)
            if proposal is None or proposal.status != ProposalStatus.AWAITING_HUMAN.value:
                return False
            proposal.status = (
                ProposalStatus.APPROVED_PAPER.value if stage == "paper" else ProposalStatus.PROMOTED.value
            )
            from datetime import datetime

            proposal.reviewed_at = datetime.now(UTC)
            self._new_version(session, proposal, stage)
            return True

    @staticmethod
    def _new_version(session, proposal: ProposalRecord, stage: str) -> None:
        from sqlalchemy import func

        strategy_id = proposal.proposed_config.get("id", "xauusd_m5_trend")
        version = (
            session.scalar(
                select(func.max(StrategyVersionRecord.version)).where(
                    StrategyVersionRecord.strategy_id == strategy_id
                )
            )
            or 0
        ) + 1
        session.add(
            StrategyVersionRecord(
                strategy_id=strategy_id,
                version=version,
                config=proposal.proposed_config,
                stage=stage,
                proposal_id=proposal.id,
            )
        )


def deep_merge(base: dict, override: dict) -> None:
    """Recursively merge ``override`` into ``base`` (in place)."""
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
