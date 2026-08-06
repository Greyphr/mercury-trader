"""Hermes reasoning service.

Pre-trade confidence assessment, post-trade reviews, and scheduled daily
analysis. All reasoning is persisted as structured JSON. Improvement proposals
enter the validation pipeline (backtest → human approval → paper → live) and
are NEVER applied to the live strategy directly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select

from mercury.core.events import Event
from mercury.core.validation import validate_against_schema
from mercury.models.orm import NewsEventRecord, ProposalRecord, ReasoningRecord, TradeRecord
from mercury.models.schemas import ProposalStatus, ReasoningKind
from mercury.services.base import Service
from mercury.services.hermes import prompts
from mercury.services.hermes.llm import LLMClient, RuleBasedClient, build_llm_client

_JSON_SAFE = (int, float, str, bool, type(None))


def _jsonable(obj: Any) -> Any:
    """Coerce arbitrary objects into JSON-safe primitives."""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, (int, float, str, bool, type(None))):
        return obj
    if hasattr(obj, "model_dump"):
        return _jsonable(obj.model_dump(mode="json"))
    try:
        return _jsonable(vars(obj))
    except TypeError:
        return str(obj)


class HermesService(Service):
    """The reasoning engine. Swappable: any class implementing the same
    event subscriptions + methods can replace it."""

    name = "hermes"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._fast_client: LLMClient = RuleBasedClient()
        self._deep_client: LLMClient = RuleBasedClient()

    # ── lifecycle ─────────────────────────────────────────────
    async def start(self) -> None:
        await super().start()
        mode = self.settings.providers.llm.mode
        llm_cfg = self.settings.providers.llm
        if mode in ("hybrid", "external"):
            self._deep_client = build_llm_client(
                provider=llm_cfg.external.provider,
            )
        if mode in ("hybrid", "local"):
            self._fast_client = build_llm_client(provider="ollama")
        if mode == "external":
            self._fast_client = self._deep_client
        if mode == "none":
            self._fast_client = RuleBasedClient()
            self._deep_client = RuleBasedClient()

        self.bus.subscribe("signal.validated", self.on_signal_validated)
        self.bus.subscribe("trade.closed", self.on_trade_closed)
        self.logger.info("hermes started", extra={"llm_mode": mode})

    # ── pre-trade assessment ──────────────────────────────────
    async def on_signal_validated(self, event: Event) -> None:
        payload = event.payload or {}
        signal = payload.get("signal")
        signal_id = payload.get("signal_id")
        if signal is None:
            return
        signal_dict = _jsonable(signal)
        market_context = self._market_context(signal.symbol, signal.timeframe)

        structured: dict[str, Any] | None = None
        try:
            structured = await self._fast_client.complete_structured(
                system=prompts.HERMES_PERSONA,
                user=prompts.pre_trade_user_prompt(signal_dict, market_context, {}),
                temperature=self.settings.providers.llm.structured.temperature,
                max_tokens=self.settings.providers.llm.structured.max_tokens,
            )
            validate_against_schema(structured, prompts.PRE_TRADE_SCHEMA)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("pre-trade LLM failed; using rule fallback", extra={"error": str(exc)})
            structured = await RuleBasedClient().complete_structured(system="", user="")
            structured["notes"] = f"LLM fallback triggered: {str(exc)[:300]}"

        confidence = float(structured.get("confidence", 0.5))
        self._persist_reasoning(
            kind=ReasoningKind.PRE_TRADE.value,
            signal_id=signal_id,
            provider=self._fast_client.name,
            confidence=confidence,
            summary=structured.get("summary", ""),
            structured=structured,
        )
        self.logger.info(
            "pre-trade assessment",
            extra={"signal_id": signal_id, "decision": structured.get("decision"),
                   "confidence": confidence},
        )
        await self.bus.publish(
            Event(
                "signal.assessed",
                {"signal": signal, "signal_id": signal_id, "assessment": structured},
            )
        )

    # ── post-trade review ─────────────────────────────────────
    async def on_trade_closed(self, event: Event) -> None:
        trade_id = event.payload.get("trade_id")
        if not trade_id:
            return
        with self.db.session() as session:
            trade = session.get(TradeRecord, trade_id)
            if trade is None:
                return
            trade_dict = _jsonable(
                {
                    "id": trade.id,
                    "symbol": trade.symbol,
                    "direction": trade.direction,
                    "volume": trade.volume,
                    "entry": trade.entry_price,
                    "exit": trade.close_price,
                    "pnl": trade.pnl,
                    "pnl_r": trade.pnl_r,
                    "close_reason": trade.close_reason,
                    "opened_at": trade.opened_at,
                    "closed_at": trade.closed_at,
                    "pre_trade_confidence": trade.pre_trade_confidence,
                }
            )
        market_context = self._market_context(trade.symbol, "M5")
        historical = self._recent_trade_summary(trade.strategy_id)

        structured = None
        try:
            structured = await self._deep_client.complete_structured(
                system=prompts.HERMES_PERSONA,
                user=prompts.post_trade_user_prompt(trade_dict, market_context, historical),
                temperature=self.settings.providers.llm.structured.temperature,
                max_tokens=self.settings.providers.llm.structured.max_tokens,
            )
            validate_against_schema(structured, prompts.POST_TRADE_SCHEMA)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("post-trade LLM failed; storing placeholder", extra={"error": str(exc)})
            structured = {"outcome_assessment": str(exc)[:300], "factors": [], "comparison": {},
                          "lessons": [], "actionable_recommendations": []}

        self._persist_reasoning(
            kind=ReasoningKind.POST_TRADE.value,
            trade_id=trade_id,
            provider=self._deep_client.name,
            summary=structured.get("outcome_assessment", ""),
            structured=structured,
        )
        self.logger.info("post-trade review stored", extra={"trade_id": trade_id})

    # ── daily analysis ────────────────────────────────────────
    async def run_daily_analysis(self) -> dict[str, Any]:
        summary = self._performance_summary()
        recent_trades = self._recent_trades(limit=30)
        news = self._recent_news(limit=20)
        structured = None
        try:
            structured = await self._deep_client.complete_structured(
                system=prompts.HERMES_PERSONA,
                user=prompts.daily_user_prompt(summary, recent_trades, news),
                temperature=self.settings.providers.llm.structured.temperature,
                max_tokens=2000,
            )
            validate_against_schema(structured, prompts.DAILY_SCHEMA)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("daily LLM failed; rule fallback", extra={"error": str(exc)})
            structured = {
                "market_summary": "Rule-based daily summary (LLM unavailable).",
                "performance_review": summary,
                "patterns_identified": [],
                "weaknesses": [],
                "opportunities": [],
                "recommendations": [],
                "proposals": [],
            }

        self._persist_reasoning(
            kind=ReasoningKind.DAILY.value,
            provider=self._deep_client.name,
            summary=structured.get("market_summary", ""),
            structured=structured,
        )
        proposals = structured.get("proposals") or []
        created = []
        for proposal in proposals:
            record = ProposalRecord(
                source="hermes",
                hypothesis=proposal.get("hypothesis", ""),
                description=proposal.get("description", ""),
                proposed_config=proposal.get("proposed_config", {}),
                status=ProposalStatus.PROPOSED.value,
            )
            with self.db.session() as session:
                session.add(record)
                session.flush()
                created.append(record.id)
        if created:
            await self.bus.publish(Event("hermes.proposals", {"proposal_ids": created}))
        self.logger.info("daily analysis complete", extra={"proposals": len(created)})
        return structured

    # ── helpers ───────────────────────────────────────────────
    def _persist_reasoning(self, *, kind: str, provider: str, structured: dict[str, Any],
                           trade_id: int | None = None, signal_id: int | None = None,
                           confidence: float | None = None, summary: str = "") -> None:
        with self.db.session() as session:
            session.add(
                ReasoningRecord(
                    kind=kind,
                    trade_id=trade_id,
                    signal_id=signal_id,
                    provider=provider,
                    confidence=confidence,
                    summary=summary,
                    structured=structured,
                )
            )

    def _market_context(self, symbol: str, timeframe: str) -> dict[str, Any]:
        from mercury.services.data.historical import load_history_from_db

        rows = load_history_from_db(self.db, symbol, timeframe, count=200)
        if not rows:
            return {"note": "no market data"}
        closes = [r["close"] for r in rows]
        return {
            "last_close": closes[-1],
            "high_200": max(r["high"] for r in rows),
            "low_200": min(r["low"] for r in rows),
            "bars": len(rows),
        }

    def _recent_trade_summary(self, strategy_id: str | None) -> dict[str, Any]:
        with self.db.session() as session:
            rows = session.scalars(
                select(TradeRecord)
                .where(TradeRecord.status == "closed")
                .order_by(TradeRecord.closed_at.desc())
                .limit(50)
            ).all()
        wins = sum(1 for t in rows if t.pnl > 0)
        return {
            "recent_count": len(rows),
            "recent_wins": wins,
            "recent_win_rate": round(wins / len(rows), 3) if rows else 0.0,
            "avg_pnl_r": round(sum(t.pnl_r for t in rows) / len(rows), 3) if rows else 0.0,
        }

    def _recent_trades(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.db.session() as session:
            rows = session.scalars(
                select(TradeRecord).order_by(TradeRecord.closed_at.desc()).limit(limit)
            ).all()
        return [
            {
                "id": t.id,
                "symbol": t.symbol,
                "direction": t.direction,
                "entry": t.entry_price,
                "exit": t.close_price,
                "pnl_r": t.pnl_r,
                "reason": t.close_reason,
                "opened": t.opened_at.isoformat() if t.opened_at else None,
                "closed": t.closed_at.isoformat() if t.closed_at else None,
            }
            for t in rows
        ]

    def _recent_news(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.db.session() as session:
            rows = session.scalars(
                select(NewsEventRecord).order_by(NewsEventRecord.event_time.desc()).limit(limit)
            ).all()
        return [{"title": n.title, "impact": n.impact, "time": str(n.event_time)} for n in rows]

    def _performance_summary(self) -> dict[str, Any]:
        from mercury.services.analytics.metrics import compute_metrics

        return compute_metrics(self.db).metrics
