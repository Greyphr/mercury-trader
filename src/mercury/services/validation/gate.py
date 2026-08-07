"""Startup validation gate.

Runs a fixed checklist before the system begins trading. Per the deployment
spec, every item on the checklist is critical: if any relevant check fails,
trading is blocked (data collection and reasoning continue). The orchestrator
applies the result by disarming the execution service and immediately notifies.

In live mode the checklist additionally requires the environment arm flag and
a working notification sink, so a misconfigured production box fails closed
rather than silently trading.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from mercury.core.logging import get_logger
from mercury.core.symbols import SymbolMappingError, get_symbol_mapper

logger = get_logger("services.validation.gate")


@dataclass
class ValidationResult:
    name: str
    ok: bool
    detail: str = ""
    relevant: bool = True


class StartupValidationGate:
    """Runs all checks and reports whether trading may proceed."""

    def __init__(self, *, settings, db, execution, collector, risk, promotion=None) -> None:
        self._settings = settings
        self._db = db
        self._execution = execution
        self._collector = collector
        self._risk = risk
        self._promotion = promotion
        self._last_results: list[ValidationResult] = []

    def run(self) -> list[ValidationResult]:
        self._last_results = [
            self._check_config(),
            self._check_database(),
            self._check_broker(),
            self._check_position_reconciliation(),
            self._check_symbols(),
            self._check_trading_arm(),
            self._check_promotion_stage(),
            self._check_kill_switch(),
            self._check_risk_config(),
            self._check_webhook_secret(),
            self._check_notifications(),
        ]
        return self._last_results

    @property
    def passed(self) -> bool:
        return all(r.ok for r in self._last_results)

    # ── checks ────────────────────────────────────────────────
    def _check_config(self) -> ValidationResult:
        env = self._settings.environment
        if not env.name:
            return self._fail("config", "no environment profile resolved")
        mapper = get_symbol_mapper(self._settings)
        unmapped = [
            s.id
            for s in self._settings.strategies.strategies
            if s.enabled
            and not _mapped(mapper, s.symbol)
        ]
        if unmapped:
            return self._fail("config", f"strategy symbols not in environment map: {unmapped}")
        return ValidationResult(
            "config",
            True,
            f"environment '{env.name}', {len(self._settings.strategies.strategies)} strategy(s)",
        )

    def _check_database(self) -> ValidationResult:
        from sqlalchemy import text

        try:
            with self._db.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return ValidationResult("database", True, "reachable")
        except Exception as exc:  # noqa: BLE001
            return self._fail("database", f"unreachable: {exc}")

    def _check_broker(self) -> ValidationResult:
        broker = self._execution.broker
        if broker is None:
            return ValidationResult(
                "broker",
                True,
                f"no broker (mode={self._settings.base.deployment.mode}, trading disabled)",
                relevant=False,
            )
        if broker.is_connected():
            return ValidationResult("broker", True, f"{type(broker).__name__} connected")
        return self._fail("broker", f"{type(broker).__name__} not connected")

    def _check_position_reconciliation(self) -> ValidationResult:
        issues = self._execution.startup_reconcile_issues
        if issues:
            return self._fail("position_reconciliation", "; ".join(issues))
        return ValidationResult("position_reconciliation", True, "broker positions match database")

    def _check_symbols(self) -> ValidationResult:
        provider = self._collector.provider
        available = provider.available_symbols()
        if not available:
            return ValidationResult("symbols", True, "no broker symbol list (paper/offline)", relevant=False)
        mapper = get_symbol_mapper(self._settings)
        verified = set(mapper.verify_available(available))
        missing = [c for c in mapper.canonical_ids() if c not in verified]
        if missing:
            return self._fail("symbols", f"preferred broker symbols missing: {missing}")
        return ValidationResult("symbols", True, f"preferred symbols verified: {sorted(verified)}")

    def _check_trading_arm(self) -> ValidationResult:
        mode = self._settings.base.deployment.mode
        if mode != "live":
            return ValidationResult("trading_arm", True, f"not required in '{mode}' mode", relevant=False)
        env = self._settings.environment
        if not env.trading_enabled:
            return self._fail("trading_arm", "live mode requires the environment arm flag (trading_enabled: true)")
        if env.broker_backend == "mt5":
            creds = env.mt5.credentials()
            if not creds["login"] or not creds["password"]:
                return self._fail("trading_arm", "live MT5 environment missing login/password credentials")
        return ValidationResult("trading_arm", True, "environment armed for live trading")

    def _check_promotion_stage(self) -> ValidationResult:
        """In live mode every enabled strategy must be at least ``approved``.

        Outside live mode the report is informational (a fresh dev/demo
        database has no lifecycle records yet), so trading is not blocked.
        """
        if self._promotion is None:
            return ValidationResult("promotion", True, "promotion service not wired", relevant=False)
        env = self._settings.environment.name
        blockers = []
        for strategy in self._settings.strategies.strategies:
            if not strategy.enabled:
                continue
            ok, required = self._promotion.may_trade_in_env(strategy.id)
            if not ok:
                stage = self._promotion.get_stage(strategy.id).value
                blockers.append(f"{strategy.id}@{stage} (need {required.value})")
        if blockers and self._settings.base.deployment.mode == "live":
            return self._fail("promotion", "strategy lifecycle blocks live trading: " + "; ".join(blockers))
        if blockers:
            return ValidationResult(
                "promotion",
                True,
                f"{len(blockers)} strategy(s) not yet at required stage for '{env}'",
                relevant=False,
            )
        return ValidationResult("promotion", True, f"all strategies approved for '{env}'")

    def _check_kill_switch(self) -> ValidationResult:
        if not self._settings.risk.kill_switch.enabled:
            return ValidationResult("kill_switch", True, "feature disabled", relevant=False)
        if self._risk.kill_switch_active():
            return self._fail("kill_switch", "global kill switch is ACTIVE")
        return ValidationResult("kill_switch", True, "off")

    def _check_risk_config(self) -> ValidationResult:
        risk = self._settings.risk
        problems: list[str] = []
        if not risk.min_risk_per_trade_percent <= risk.risk_per_trade_percent <= risk.max_risk_per_trade_percent:
            problems.append("risk_per_trade_percent outside [min, max]")
        if risk.sizing.contract_size <= 0:
            problems.append("contract_size must be > 0")
        if risk.sizing.mode == "fixed_percent" and risk.sizing.fixed_percent <= 0:
            problems.append("fixed_percent must be > 0")
        if risk.guards.max_open_positions < 1:
            problems.append("max_open_positions must be >= 1")
        if problems:
            return self._fail("risk_config", "; ".join(problems))
        return ValidationResult("risk_config", True, "sane")

    def _check_webhook_secret(self) -> ValidationResult:
        if self._settings.base.deployment.mode != "live":
            return ValidationResult("webhook_secret", True, "not required outside live mode", relevant=False)
        if "tradingview" not in self._settings.providers.signal.providers:
            return ValidationResult("webhook_secret", True, "webhook provider not enabled", relevant=False)
        secret = self._settings.providers.signal.webhook.secret
        if secret:
            return ValidationResult("webhook_secret", True, "tradingview webhook secret configured")
        return self._fail(
            "webhook_secret",
            "tradingview webhook enabled in live mode but SIGNAL_WEBHOOK_SECRET is missing",
        )

    def _check_notifications(self) -> ValidationResult:
        if self._settings.base.deployment.mode != "live":
            return ValidationResult("notifications", True, "not required outside live mode", relevant=False)
        backend = self._settings.providers.notifications.backend
        if backend == "telegram":
            token = os.getenv("TELEGRAM_BOT_TOKEN", "")
            chat_id = self._settings.providers.notifications.telegram.chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
            if token and chat_id:
                return ValidationResult("notifications", True, "telegram configured")
            return self._fail("notifications", "telegram backend enabled but TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing")
        return ValidationResult("notifications", True, f"backend: {backend}")

    @staticmethod
    def _fail(name: str, detail: str) -> ValidationResult:
        logger.error("startup validation check failed", extra={"check": name, "detail": detail})
        return ValidationResult(name, False, detail)


def _mapped(mapper, symbol: str) -> bool:
    try:
        mapper.broker_symbol(symbol)
        return True
    except SymbolMappingError:
        return False
