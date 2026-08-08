from datetime import datetime

import pytest

from mercury.core.events import EventBus
from mercury.models.orm import MetricsRecord, TradeRecord
from mercury.services.analytics.metrics import compute_metrics, compute_metrics_snapshot
from mercury.services.analytics.service import AnalyticsService


def _insert_trade(db, *, pnl=0.0, pnl_r=0.0, status="closed", direction="long", close_reason="tp"):
    with db.session() as session:
        session.add(
            TradeRecord(
                ticket=f"t-{pnl}-{direction}-{close_reason}",
                symbol="XAUUSD",
                direction=direction,
                volume=0.1,
                entry_price=2400.0,
                sl=2395.0,
                tp=2410.0,
                status=status,
                pnl=pnl,
                pnl_r=pnl_r,
                close_reason=close_reason,
                deployment_mode="paper",
            )
        )


def test_metrics_with_no_trades(db):
    metrics = compute_metrics(db)
    assert metrics["total_trades"] == 0
    assert metrics["win_rate"] == 0.0


def test_metrics_win_rate_and_pf(db):
    _insert_trade(db, pnl=50.0, pnl_r=2.0, close_reason="tp")
    _insert_trade(db, pnl=-20.0, pnl_r=-0.8, close_reason="sl")
    _insert_trade(db, pnl=30.0, pnl_r=1.2, close_reason="tp")
    metrics = compute_metrics(db)
    assert metrics["total_trades"] == 3
    assert metrics["win_rate"] == pytest.approx(2 / 3, abs=0.001)
    assert metrics["profit_factor"] == pytest.approx(80 / 20)


def test_metrics_drawdown(db):
    _insert_trade(db, pnl=50.0, pnl_r=2.0, close_reason="tp")
    _insert_trade(db, pnl=-100.0, pnl_r=-4.0, close_reason="sl")
    metrics = compute_metrics(db)
    assert metrics["max_drawdown_percent"] > 0


def test_open_trades_excluded(db):
    _insert_trade(db, pnl=0.0, pnl_r=0.0, status="open")
    metrics = compute_metrics(db)
    assert metrics["total_trades"] == 0


@pytest.mark.asyncio
async def test_tick_records_metrics_with_non_null_as_of(db, settings):
    svc = AnalyticsService(bus=EventBus(), settings=settings, db=db)
    await svc.tick()

    with db.session() as session:
        records = list(session.query(MetricsRecord).all())

    assert len(records) == 1
    record = records[0]
    assert record.period == "periodic"
    assert record.as_of is not None
    assert record.as_of == datetime.fromisoformat(record.metrics["as_of"]).replace(tzinfo=None)


def test_compute_metrics_snapshot_records_as_of(db):
    metrics = compute_metrics_snapshot(db, period="daily")

    with db.session() as session:
        records = list(session.query(MetricsRecord).all())

    assert len(records) == 1
    record = records[0]
    assert record.period == "daily"
    assert record.as_of == datetime.fromisoformat(metrics["as_of"]).replace(tzinfo=None)
