from pydantic import ValidationError

from mercury.core.config import Settings, load_config


def test_config_loads(settings):
    assert isinstance(settings, Settings)
    assert settings.base.project.name == "Mercury Trader"
    assert settings.base.deployment.mode in ("live", "paper", "read_only")
    assert len(settings.strategies.strategies) >= 1


def test_config_defaults_paper(settings):
    assert settings.base.deployment.mode == "paper"


def test_risk_defaults(settings):
    risk = settings.risk
    assert 0 < risk.risk_per_trade_percent <= risk.max_risk_per_trade_percent
    assert risk.guards.max_open_positions >= 1


def test_strategy_config(settings):
    strategy = settings.strategies.strategies[0]
    assert strategy.symbol == "GOLD"
    assert strategy.timeframe == "M5"
    assert strategy.order.magic == 77001


def test_invalid_deployment_mode_rejected():
    from mercury.core.config import DeploymentConfig

    try:
        DeploymentConfig(mode="bad")
        raise AssertionError("should have raised")
    except ValidationError:
        pass


def test_database_url_default():
    s = load_config()
    assert s.database_url.startswith("postgresql")


def test_blank_database_url_falls_back_to_environment_default(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    s = load_config()
    assert s.database_url == "postgresql+psycopg://mercury:mercury@localhost:5432/mercury_development"


def test_postgres_engine_has_connect_timeout():
    from mercury.core.db import Database

    db = Database("postgresql+psycopg://x:y@localhost:5432/z")
    try:
        import inspect

        params = inspect.getclosurevars(db.engine.pool._creator).nonlocals.get("cparams", {})
        assert params.get("connect_timeout") == 10
    finally:
        db.dispose()
