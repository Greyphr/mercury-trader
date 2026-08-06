import pytest

from mercury.models.orm import TradeRecord
from mercury.services.analytics.metrics import compute_metrics


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
