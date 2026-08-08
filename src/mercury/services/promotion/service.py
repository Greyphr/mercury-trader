"""Strategy promotion workflow.

Owns the DB-tracked deployment lifecycle of every strategy:

    draft → paper → demo → review → approved → live

A strategy may only move **one stage forward at a time** (no skipping) and
must be promoted explicitly via CLI (or API). Promotions are audited in
``strategy_promotions``; the current stage lives in ``strategy_lifecycle``.

Live trading is blocked until a strategy is ``approved``: the startup
validation gate (live environments) and the execution choke point both refuse
orders from strategies whose lifecycle stage is not ready for the current
environment. Outside live mode the stage check is informational, so a fresh
development/demo database can still run while operators promote strategies.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from mercury.core.events import Event
from mercury.models.orm import StrategyLifecycleRecord, StrategyPromotionRecord
from mercury.models.schemas import StrategyStage
from mercury.services.base import Service

_ORDER = [
    StrategyStage.DRAFT,
    StrategyStage.PAPER,
    StrategyStage.DEMO,
    StrategyStage.REVIEW,
    StrategyStage.APPROVED,
    StrategyStage.LIVE,
]

# The only allowed one-stage forward moves (no skipping allowed).
_ALLOWED_FORWARD: dict[StrategyStage, set[StrategyStage]] = {
    StrategyStage.DRAFT: {StrategyStage.PAPER},
    StrategyStage.PAPER: {StrategyStage.DEMO},
    StrategyStage.DEMO: {StrategyStage.REVIEW},
    StrategyStage.REVIEW: {StrategyStage.APPROVED},
    StrategyStage.APPROVED: {StrategyStage.LIVE},
    StrategyStage.LIVE: set(),
}

# Stages that require an explicit human gate (actor + reason).
_HUMAN_GATED = {StrategyStage.APPROVED, StrategyStage.LIVE}

# Minimum stage required to trade in each environment profile.
_ENV_REQUIREMENT: dict[str, StrategyStage] = {
    "development": StrategyStage.PAPER,
    "metaquotes_demo": StrategyStage.DEMO,
    "exness_live": StrategyStage.APPROVED,
}


def _rank(stage: StrategyStage) -> int:
    return _ORDER.index(stage)


class PromotionError(ValueError):
    """Raised when a promotion/demotion is not allowed."""


class PromotionService(Service):
    """Tracks and enforces strategy lifecycle transitions."""

    name = "promotion"

    async def start(self) -> None:
        await super().start()
        self.mark_healthy(f"pipeline: {' > '.join(s.value for s in _ORDER)}")

    # ── reads ─────────────────────────────────────────────────
    def get_stage(self, strategy_id: str) -> StrategyStage:
        """Current lifecycle stage (defaults to ``draft`` when unknown)."""
        with self.db.session() as session:
            record = session.get(StrategyLifecycleRecord, strategy_id)
        return StrategyStage(record.stage) if record else StrategyStage.DRAFT

    def history(self, strategy_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """Most recent promotion audit records for a strategy."""
        with self.db.session() as session:
            rows = session.scalars(
                select(StrategyPromotionRecord)
                .where(StrategyPromotionRecord.strategy_id == strategy_id)
                .order_by(StrategyPromotionRecord.created_at.desc())
                .limit(limit)
            ).all()
        return [
            {
                "id": r.id,
                "from_stage": r.from_stage,
                "to_stage": r.to_stage,
                "actor": r.actor,
                "reason": r.reason,
                "metrics": r.metrics or {},
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]

    def required_stage(self, environment: str | None = None) -> StrategyStage:
        """Minimum stage a strategy must reach to trade in ``environment``."""
        name = environment or self.settings.environment.name
        return _ENV_REQUIREMENT.get(name, StrategyStage.PAPER)

    def may_trade_in_env(self, strategy_id: str) -> tuple[bool, StrategyStage]:
        """Return ``(ok, required_stage)`` for the current environment."""
        required = self.required_stage()
        return _rank(self.get_stage(strategy_id)) >= _rank(required), required

    def stage_guard(self, strategy_id: str) -> bool:
        """Choke point used by the execution service.

        Only enforced in live mode (the startup gate handles non-live
        environments informationally), so a demotion while running blocks
        further orders immediately.
        """
        if self.settings.deployment_mode != "live":
            return True
        return _rank(self.get_stage(strategy_id)) >= _rank(StrategyStage.APPROVED)

    # ── transitions ───────────────────────────────────────────
    def promote(
        self,
        strategy_id: str,
        to_stage: StrategyStage | str,
        *,
        actor: str = "cli",
        reason: str = "",
        metrics: dict[str, Any] | None = None,
        check_gates: bool = False,
    ) -> StrategyStage:
        """Move a strategy exactly one stage forward (or raise).

        ``check_gates=True`` validates ``metrics`` against the configured
        promotion gates for the target stage before committing.
        """
        target = StrategyStage(to_stage)
        current = self.get_stage(strategy_id)
        allowed = _ALLOWED_FORWARD.get(current, set())
        if target not in allowed:
            raise PromotionError(
                f"cannot promote '{strategy_id}' from '{current.value}' to '{target.value}' "
                f"(allowed forward moves: {sorted(s.value for s in allowed) or 'none'})"
            )
        if target in _HUMAN_GATED and (not actor or not reason):
            raise PromotionError(
                f"promoting to '{target.value}' requires an actor and a reason (manual approval gate)"
            )
        if check_gates and target in {StrategyStage.DEMO, StrategyStage.LIVE}:
            ok, failures = self.validate_metrics(metrics or {}, target=target)
            if not ok:
                raise PromotionError("promotion gate not met: " + "; ".join(failures))
        self._transition(strategy_id, current, target, actor, reason, metrics)
        return target

    def approve(
        self,
        strategy_id: str,
        *,
        actor: str,
        reason: str,
        metrics: dict[str, Any] | None = None,
    ) -> StrategyStage:
        """Explicit human approval gate: ``review → approved``."""
        return self.promote(
            strategy_id, StrategyStage.APPROVED, actor=actor, reason=reason, metrics=metrics
        )

    def demote(
        self,
        strategy_id: str,
        to_stage: StrategyStage | str,
        *,
        actor: str = "cli",
        reason: str = "",
    ) -> StrategyStage:
        """Roll a strategy back to an earlier stage (draft is the reset)."""
        target = StrategyStage(to_stage)
        current = self.get_stage(strategy_id)
        if _rank(target) >= _rank(current):
            raise PromotionError(
                f"cannot demote '{strategy_id}' to '{target.value}' (not earlier than current '{current.value}')"
            )
        self._transition(strategy_id, current, target, actor, reason or f"demoted to {target.value}", None)
        return target

    def reset(self, strategy_id: str, *, actor: str = "cli", reason: str = "reset to draft") -> StrategyStage:
        return self.demote(strategy_id, StrategyStage.DRAFT, actor=actor, reason=reason)

    # ── metrics gates ─────────────────────────────────────────
    def validate_metrics(
        self, metrics: dict[str, Any], *, target: StrategyStage
    ) -> tuple[bool, list[str]]:
        """Check ``metrics`` against trading_criteria.promotion_gates."""
        gates_cfg = self.settings.trading_criteria.promotion_gates
        if gates_cfg is None:
            return True, []
        gate_key = "live" if target in {StrategyStage.APPROVED, StrategyStage.LIVE} else "paper"
        gate = getattr(gates_cfg, gate_key, None)
        if gate is None:
            return True, []
        failures: list[str] = []
        if metrics.get("trades", 0) < gate.min_trades:
            failures.append(f"trades {metrics.get('trades')} < {gate.min_trades}")
        if metrics.get("win_rate", 0.0) < gate.min_win_rate:
            failures.append(f"win_rate {metrics.get('win_rate')} < {gate.min_win_rate}")
        if metrics.get("profit_factor", 0.0) < gate.min_profit_factor:
            failures.append(f"profit_factor {metrics.get('profit_factor')} < {gate.min_profit_factor}")
        if metrics.get("max_drawdown_percent", 0.0) > gate.max_drawdown_percent:
            failures.append(
                f"max_drawdown_percent {metrics.get('max_drawdown_percent')} > {gate.max_drawdown_percent}"
            )
        return not failures, failures

    # ── persistence ───────────────────────────────────────────
    def _transition(
        self,
        strategy_id: str,
        current: StrategyStage,
        target: StrategyStage,
        actor: str,
        reason: str,
        metrics: dict[str, Any] | None,
    ) -> None:
        with self.db.session() as session:
            record = session.get(StrategyLifecycleRecord, strategy_id)
            if record is None:
                record = StrategyLifecycleRecord(strategy_id=strategy_id, stage=target.value, meta={})
                session.add(record)
            else:
                record.stage = target.value
                record.updated_at = datetime.now(UTC)
            session.add(
                StrategyPromotionRecord(
                    strategy_id=strategy_id,
                    from_stage=current.value,
                    to_stage=target.value,
                    actor=actor,
                    reason=reason,
                    metrics=metrics or {},
                )
            )

        self.logger.info(
            "strategy promoted",
            extra={"strategy": strategy_id, "from": current.value, "to": target.value, "actor": actor},
        )
        if self.bus is not None:
            self.bus.publish_nowait(
                Event(
                    "strategy.promoted",
                    {
                        "strategy_id": strategy_id,
                        "from_stage": current.value,
                        "to_stage": target.value,
                        "actor": actor,
                        "reason": reason,
                    },
                )
            )
