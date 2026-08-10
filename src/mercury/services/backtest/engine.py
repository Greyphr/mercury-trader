"""Lightweight event-driven backtest engine.

Simulates entries from a strategy's signals on closed candles, then walks
forward bar-by-bar evaluating intrabar TP/SL hits. Conservative exit rule:
when both TP and SL are touched within one bar, SL is assumed hit first.

Used by the Hermes improvement pipeline to validate proposals before any
human or paper-trading approval.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from mercury.core.logging import get_logger
from mercury.core.validation import Candle
from mercury.models.schemas import Direction
from mercury.services.strategy import indicators as ind
from mercury.services.strategy.strategies import Strategy

logger = get_logger("services.backtest.engine")


@dataclass
class SimulatedTrade:
    entry_time: datetime
    exit_time: datetime
    direction: str
    entry: float
    exit: float
    sl: float
    tp: float
    pnl_r: float
    close_reason: str


@dataclass
class BacktestOutput:
    strategy_id: str
    start: datetime
    end: datetime
    initial_equity: float
    final_equity: float
    trades: list[SimulatedTrade] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_result(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "initial_equity": self.initial_equity,
            "final_equity": round(self.final_equity, 2),
            "trades": len(self.trades),
            "metrics": self.metrics,
            "net_profit": round(self.final_equity - self.initial_equity, 2),
        }


def build_strategy_for_backtest(strategy_cfg, settings) -> Strategy:
    """Instantiate the configured strategy for backtesting.

    ICT and EMA+trendline confluence strategies get a context provider that
    pulls H1/H4 history from the live data provider (MT5 when available,
    synthetic otherwise).
    """
    from mercury.services.data.historical import load_history
    from mercury.services.strategy.ict import ICTStrategy
    from mercury.services.strategy.strategies import build_strategies
    from mercury.services.strategy.trend_confluence import TrendConfluenceStrategy

    strategy = build_strategies([strategy_cfg], settings=settings)[0]
    if isinstance(strategy, (ICTStrategy, TrendConfluenceStrategy)):
        strategy.set_context_provider(
            lambda symbol, timeframe, count: [
                Candle.model_validate(c) for c in load_history(settings, symbol, timeframe, count=count)
            ]
        )
    return strategy


def run_backtest(
    strategy: Strategy,
    candles: list[Candle],
    *,
    initial_equity: float = 10_000.0,
    risk_percent: float = 0.5,
    contract_size: float = 100.0,
) -> BacktestOutput:
    """Run an event-driven backtest over closed candles for a strategy.

    For ICT strategies the configured management rules are simulated:
    move SL to breakeven at ``be_at_r`` (default 1R) and exit early when an
    opposite M5 break-of-structure forms (default on). For EMA+trendline
    confluence strategies the safety line is trailed each bar and the trade
    exits when a candle closes beyond it (no take-profit).
    """
    if len(candles) < 50:
        raise ValueError("not enough candles for backtest")

    ict = getattr(strategy.config, "ict", None)
    be_at_r = ict.management.breakeven_at_r if ict is not None else None
    exit_on_opposite_bos = bool(ict and ict.management.early_exit_on_opposite_bos)
    trendline = getattr(strategy.config, "trendline", None)
    is_trendline = trendline is not None
    sl_buffer = trendline.sl_buffer_atr if trendline else 0.0

    signals = strategy.generate_signals(candles)
    time_index = {c.time: i for i, c in enumerate(candles)}

    equity = initial_equity
    trades: list[SimulatedTrade] = []
    open_trade: dict[str, Any] | None = None

    for signal in signals:
        if open_trade is not None:
            continue  # single position at a time
        sig_idx = time_index.get(_signal_time(signal))
        if sig_idx is None or sig_idx + 1 >= len(candles):
            continue
        entry_idx = sig_idx + 1
        entry_bar = candles[entry_idx]
        entry_price = entry_bar.open
        sl = signal.sl or 0.0
        tp = signal.tp or 0.0
        if sl <= 0:
            continue
        if not is_trendline and tp <= 0:
            continue

        risk_per_unit = abs(entry_price - sl)
        if risk_per_unit <= 0:
            continue
        volume = (equity * (risk_percent / 100.0)) / (risk_per_unit * contract_size)
        risk_amount = equity * (risk_percent / 100.0)
        direction = 1 if signal.direction == Direction.LONG else -1

        obos_idx: int | None = None
        if exit_on_opposite_bos:
            rel = ind.detect_opposite_bos(candles[entry_idx:], signal.direction.value)
            if rel is not None:
                obos_idx = entry_idx + rel

        closed: tuple[str, float] | None = None
        exit_idx = entry_idx
        breakeven_triggered = False
        if is_trendline:
            # Trendline confluence: no TP — trail the safety line and exit on
            # the first candle that closes beyond it (SL still honored first).
            for j in range(entry_idx, len(candles)):
                bar = candles[j]
                if direction == 1 and bar.low <= sl:
                    closed = ("sl", sl)
                    exit_idx = j
                    break
                if direction == -1 and bar.high >= sl:
                    closed = ("sl", sl)
                    exit_idx = j
                    break
                safety = strategy.safety_value(bar.time, signal.direction)
                if safety is None:
                    continue
                if direction == 1 and bar.close < safety:
                    closed = ("safety_line", bar.close)
                    exit_idx = j
                    break
                if direction == -1 and bar.close > safety:
                    closed = ("safety_line", bar.close)
                    exit_idx = j
                    break
                candidate = (safety - sl_buffer) if direction == 1 else (safety + sl_buffer)
                if direction == 1 and candidate > sl:
                    sl = candidate
                elif direction == -1 and candidate < sl:
                    sl = candidate
        else:
            for j in range(entry_idx, len(candles)):
                bar = candles[j]
                if direction == 1:
                    if bar.low <= sl:
                        closed = ("sl", sl)
                        exit_idx = j
                        break
                    if bar.high >= tp:
                        closed = ("tp", tp)
                        exit_idx = j
                        break
                else:
                    if bar.high >= sl:
                        closed = ("sl", sl)
                        exit_idx = j
                        break
                    if bar.low <= tp:
                        closed = ("tp", tp)
                        exit_idx = j
                        break
                if obos_idx is not None and j == obos_idx:
                    exit_price = bar.close
                    if breakeven_triggered:
                        exit_price = max(exit_price, entry_price) if direction == 1 else min(exit_price, entry_price)
                    closed = ("bos", exit_price)
                    exit_idx = j
                    break
                if be_at_r and not breakeven_triggered:
                    if direction == 1 and bar.high >= entry_price + be_at_r * risk_per_unit:
                        sl = entry_price
                        breakeven_triggered = True
                    elif direction == -1 and bar.low <= entry_price - be_at_r * risk_per_unit:
                        sl = entry_price
                        breakeven_triggered = True
        if closed is None:
            continue  # never closed within sample

        reason, exit_price = closed
        pnl = direction * (exit_price - entry_price) * contract_size * volume
        equity += pnl
        trades.append(
            SimulatedTrade(
                entry_time=entry_bar.time,
                exit_time=candles[exit_idx].time if exit_idx < len(candles) else datetime.now(UTC),
                direction=signal.direction.value,
                entry=entry_price,
                exit=exit_price,
                sl=signal.sl or 0.0,
                tp=tp,
                pnl_r=round(pnl / risk_amount, 4) if risk_amount else 0.0,
                close_reason=reason,
            )
        )
        open_trade = {"entry_idx": entry_idx, "tp": tp, "sl": sl, "direction": direction}

    return BacktestOutput(
        strategy_id=strategy.id,
        start=candles[0].time,
        end=candles[-1].time,
        initial_equity=initial_equity,
        final_equity=equity,
        trades=trades,
        metrics=_metrics_from_trades(trades, initial_equity, final_equity=equity),
    )


def _metrics_from_trades(trades: list[SimulatedTrade], initial: float, final_equity: float) -> dict[str, Any]:
    wins = [t for t in trades if t.pnl_r > 0]
    losses = [t for t in trades if t.pnl_r < 0]
    win_rate = len(wins) / len(trades) if trades else 0.0
    gross_win = sum(t.pnl_r for t in wins)
    gross_loss = abs(sum(t.pnl_r for t in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)
    expectancy = (sum(t.pnl_r for t in trades) / len(trades)) if trades else 0.0

    # Drawdown on cumulative R.
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        cum += t.pnl_r
        peak = max(peak, cum)
        max_dd = max(max_dd, (peak - cum) if peak > 0 else 0.0)

    return {
        "trades": len(trades),
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 4) if profit_factor != float("inf") else None,
        "expectancy_r": round(expectancy, 4),
        "max_drawdown_r": round(max_dd, 4),
        "net_pnl_r": round(cum, 4),
        "return_percent": round((final_equity - initial) / initial * 100.0, 4) if initial else 0.0,
    }


def _signal_time(signal) -> datetime:
    meta_time = signal.meta.get("candle_time")
    if meta_time:
        try:
            return datetime.fromisoformat(meta_time)
        except ValueError:
            pass
    return signal.created_at
