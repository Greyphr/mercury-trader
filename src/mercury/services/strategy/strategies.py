"""Configurable rule-based strategies.

Strategies are defined declaratively in ``config/strategy_xauusd_m5.yaml`` and
evaluated on confirmed (closed) candles. Each strategy produces :class:`Signal`
candidates that flow to Hermes for confidence assessment and to the risk
manager for validation.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any

import numpy as np

from mercury.core.config import StrategyConfig
from mercury.core.validation import Candle
from mercury.models.schemas import Direction, Signal, SignalSource
from mercury.services.strategy import indicators as ind


class Strategy(ABC):
    """Base strategy interface."""

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    @property
    def id(self) -> str:
        return self.config.id

    @abstractmethod
    def generate_signals(self, candles: list[Candle]) -> list[Signal]:
        """Return signal candidates from a series of CLOSED candles."""
        raise NotImplementedError


def _atr_extreme_flag(atr_vals: np.ndarray, atr_mean: np.ndarray, max_mult: float, i: int) -> bool:
    return not np.isnan(atr_mean[i]) and atr_vals[i] > max_mult * atr_mean[i]


class TrendFollowingStrategy(Strategy):
    """EMA cross + trend filter + RSI momentum + ATR-based stops."""

    def generate_signals(self, candles: list[Candle]) -> list[Signal]:
        cfg = self.config.entry
        order = self.config.order
        if len(candles) < cfg.trend_ema_period + 20:
            return []

        closes = ind.closes(candles)
        ema_trend = ind.ema(closes, cfg.trend_ema_period)
        rsi_vals = ind.rsi(closes, cfg.rsi_period)
        atr_vals = ind.atr(candles, cfg.atr_period)
        atr_mean = ind.sma(atr_vals, 100)
        crosses = ind.ema_cross_signal(closes, cfg.fast_ema_period, cfg.slow_ema_period)

        signals: list[Signal] = []
        warmup = max(cfg.slow_ema_period, cfg.trend_ema_period, cfg.atr_period) + 2

        for i in range(warmup, len(candles)):
            c = candles[i]
            if crosses[i] is None or np.isnan(ema_trend[i]):
                continue
            if np.isnan(rsi_vals[i]) or np.isnan(atr_vals[i]):
                continue

            crossed_up = crosses[i] == "up"
            crossed_down = crosses[i] == "down"

            long_ok = (
                crossed_up
                and c.close > ema_trend[i]
                and rsi_vals[i] < cfg.rsi_buy_max
                and not _atr_extreme_flag(atr_vals, atr_mean, cfg.max_atr_multiplier, i)
            )
            short_ok = (
                crossed_down
                and c.close < ema_trend[i]
                and rsi_vals[i] > cfg.rsi_sell_min
                and not _atr_extreme_flag(atr_vals, atr_mean, cfg.max_atr_multiplier, i)
            )

            direction: Direction | None = None
            if long_ok and order.direction in ("long", "both"):
                direction = Direction.LONG
            elif short_ok and order.direction in ("short", "both"):
                direction = Direction.SHORT

            if direction is None:
                continue

            atr_i = float(atr_vals[i])
            if order.use_atr_levels and atr_i > 0:
                sl_dist = order.sl_atr_multiplier * atr_i
                tp_dist = order.tp_atr_multiplier * atr_i
            else:
                pip = order.pip_size
                sl_dist = order.sl_pips * pip
                tp_dist = order.tp_pips * pip

            entry = c.close
            if direction == Direction.LONG:
                sl = round(entry - sl_dist, 2)
                tp = round(entry + tp_dist, 2)
            else:
                sl = round(entry + sl_dist, 2)
                tp = round(entry - tp_dist, 2)

            signals.append(
                Signal(
                    provider=SignalSource.INTERNAL_STRATEGY,
                    strategy_id=self.config.id,
                    symbol=self.config.symbol,
                    timeframe=self.config.timeframe,
                    direction=direction,
                    price=entry,
                    sl=sl,
                    tp=tp,
                    meta={
                        "candle_time": c.time.isoformat(),
                        "rsi": float(rsi_vals[i]),
                        "atr": atr_i,
                        "fast_ema": float(ema_fast[i]),
                        "slow_ema": float(ema_slow[i]),
                    },
                    created_at=datetime.now(UTC),
                )
            )
        return signals


def build_strategies(strategy_configs: list[StrategyConfig], settings: Any | None = None) -> list[Strategy]:
    """Instantiate strategy objects from config.

    ICT/SMC and EMA+trendline confluence strategies (those declaring an
    ``ict`` or ``trendline`` block) need a higher-timeframe context provider
    plus the base settings (for trading sessions); the caller attaches the
    provider via ``set_context_provider``.
    """
    from mercury.services.strategy.ict import ICTStrategy
    from mercury.services.strategy.trend_confluence import TrendConfluenceStrategy

    strategies: list[Strategy] = []
    for cfg in strategy_configs:
        if not cfg.enabled:
            continue
        if cfg.ict is not None:
            strategies.append(ICTStrategy(cfg, settings=settings))
        elif cfg.trendline is not None:
            strategies.append(TrendConfluenceStrategy(cfg, settings=settings))
        else:
            strategies.append(TrendFollowingStrategy(cfg))
    return strategies
