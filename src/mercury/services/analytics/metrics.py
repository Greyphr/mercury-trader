"""Performance metrics computation over the persisted trade ledger.

Produces the evaluation metrics configured in ``trading_criteria.yaml``:
win rate, profit factor, expectancy (R), max drawdown, Sharpe/Sortino, etc.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from mercury.models.orm import TradeRecord


def _closed_trades(db):
    with db.session() as session:
        return session.scalars(
            select(TradeRecord).where(TradeRecord.status == "closed").order_by(TradeRecord.closed_at)
        ).all()


def compute_metrics(db, *, since: datetime | None = None) -> dict[str, Any]:
    """Compute performance metrics over closed trades."""
    trades = _closed_trades(db)
    if since is not None:
        trades = [t for t in trades if t.closed_at and t.closed_at >= since]

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl < 0]
    breakeven = [t for t in trades if t.pnl == 0]

    total_pnl = sum(t.pnl for t in trades)
    win_rate = len(wins) / len(trades) if trades else 0.0
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)

    r_values = [t.pnl_r for t in trades if t.pnl_r]
    expectancy_r = sum(r_values) / len(r_values) if r_values else 0.0
    avg_win_r = sum(t.pnl_r for t in wins if t.pnl_r) / len(wins) if wins else 0.0
    avg_loss_r = sum(t.pnl_r for t in losses if t.pnl_r) / len(losses) if losses else 0.0

    # Equity curve from realized P&L.
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    max_dd_duration = 0.0
    current_dd_start: datetime | None = None
    pnl_series: list[float] = []
    for t in trades:
        equity += t.pnl
        pnl_series.append(t.pnl)
        if equity > peak:
            peak = equity
            current_dd_start = None
        else:
            dd = (peak - equity) / peak * 100.0 if peak > 0 else 0.0
            max_dd = max(max_dd, dd)
            if current_dd_start is None:
                current_dd_start = t.closed_at or datetime.now(UTC)
            if current_dd_start:
                duration = ((t.closed_at or datetime.now(UTC)) - current_dd_start).days
                max_dd_duration = max(max_dd_duration, duration)

    # Sharpe / Sortino on per-trade returns (annualization via sqrt(trades)).
    if len(pnl_series) > 1:
        mean = sum(pnl_series) / len(pnl_series)
        std = math.sqrt(sum((x - mean) ** 2 for x in pnl_series) / (len(pnl_series) - 1))
        downside = [x for x in pnl_series if x < 0]
        d_std = math.sqrt(sum(x * x for x in downside) / len(downside)) if downside else 0.0
        sharpe = (mean / std) * math.sqrt(len(pnl_series)) if std > 0 else 0.0
        sortino = (mean / d_std) * math.sqrt(len(pnl_series)) if d_std > 0 else 0.0
    else:
        sharpe = sortino = 0.0

    consecutive_losses = 0
    current = 0
    for t in trades:
        if t.pnl < 0:
            current += 1
            consecutive_losses = max(consecutive_losses, current)
        else:
            current = 0

    holds = [t for t in trades if t.opened_at and t.closed_at]
    avg_hold_min = (
        sum((t.closed_at - t.opened_at).total_seconds() / 60 for t in holds) / len(holds) if holds else 0.0
    )

    return {
        "as_of": datetime.now(UTC).isoformat(),
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(breakeven),
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 4) if math.isfinite(profit_factor) else None,
        "expectancy_r": round(expectancy_r, 4),
        "average_win_r": round(avg_win_r, 4),
        "average_loss_r": round(avg_loss_r, 4),
        "total_pnl": round(total_pnl, 2),
        "max_drawdown_percent": round(max_dd, 4),
        "max_drawdown_duration_days": round(max_dd_duration, 2),
        "sharpe_ratio": round(sharpe, 4),
        "sortino_ratio": round(sortino, 4),
        "consecutive_losses": consecutive_losses,
        "average_holding_minutes": round(avg_hold_min, 2),
        "breakeven_trades_count": len(breakeven),
    }


def compute_metrics_snapshot(db, *, period: str) -> dict[str, Any]:
    """Wrapper that also records the snapshot to the DB."""
    from mercury.models.orm import MetricsRecord

    metrics = compute_metrics(db)
    with db.session() as session:
        session.add(
            MetricsRecord(
                period=period,
                as_of=datetime.fromisoformat(metrics["as_of"]),
                metrics=metrics,
            )
        )
    return metrics
