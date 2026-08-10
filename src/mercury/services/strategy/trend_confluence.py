"""Merged EMA + trendline "Action Line / Safety Line" confluence strategy.

Entry requires two conditions on the same closed candle that must agree on
direction:

* an EMA cross on the strategy timeframe (fast/slow from ``entry``), and
* an action-line break of a chained primary-timeframe trendline
  (support line break → SHORT, resistance line break → LONG).

There is no fixed take-profit. The stop is the opposing "safety line": for a
short the nearest downtrend line above price, for a long the nearest uptrend
line below price. The initial stop is placed just beyond it with an ATR
buffer; every management cycle the stop trails the (sloped) line, never
against the position, and the trade exits when a candle closes beyond it.

Only confirmed (closed) candles are used, and every evaluation only sees
primary/bias-timeframe candles that have fully closed by that point in time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from mercury.core.config import TrendlineConfig, TradingSession
from mercury.core.validation import Candle
from mercury.models.schemas import Direction, Signal, SignalSource
from mercury.services.strategy import indicators as ind
from mercury.services.strategy.ict import ContextProvider, _session_name
from mercury.services.strategy.strategies import Strategy

_TF_HOURS = {
    "M1": 1 / 60,
    "M5": 1 / 12,
    "M15": 0.25,
    "M30": 0.5,
    "H1": 1.0,
    "H2": 2.0,
    "H4": 4.0,
    "D1": 24.0,
    "D": 24.0,
}


def _closed_by(timeframe: str, t: datetime) -> datetime:
    """Latest time before ``t`` at which a ``timeframe`` candle has closed.

    A candle whose ``time`` is its open time is closed at open time + its
    duration, so a candle may be used only when ``time <= t - duration``.
    """
    return t - timedelta(hours=_TF_HOURS.get(timeframe.upper(), 1.0))


def _last_line(lines: list[dict], kind: str) -> dict | None:
    for line in reversed(lines):
        if line["type"] == kind:
            return line
    return None


class TrendConfluenceStrategy(Strategy):
    """EMA cross + action-line trendline break confluence; exits trail the
    opposing safety line with no fixed take-profit."""

    def __init__(self, config, *, context_provider: ContextProvider | None = None,
                 settings: Any | None = None) -> None:
        super().__init__(config)
        self._provider = context_provider
        self._settings = settings
        self._line_cache: dict[tuple[str, datetime], tuple[list[dict], int]] = {}

    @property
    def sessions(self) -> list[TradingSession]:
        if self._settings is not None:
            return self._settings.base.trading_sessions
        return []

    def set_context_provider(self, provider: ContextProvider) -> None:
        self._provider = provider

    # ── HTF context ───────────────────────────────────────────
    def _lines_as_of(self, t: datetime) -> tuple[list[dict], int]:
        """Chained primary-timeframe trendlines closed at or before ``t``.

        Returns ``(lines, last_h1_idx)``. The primary-timeframe prefix only
        changes once per its candle close, so the result is memoized by the
        prefix's last candle time (mirrors the ICT snapshot caching).
        """
        cfg = self.config.trendline
        if cfg is None or self._provider is None:
            return [], -1
        rows = self._provider(self.config.symbol, cfg.timeframe, cfg.bars)
        h1 = [c for c in rows if c.symbol == self.config.symbol and c.time <= _closed_by(cfg.timeframe, t)]
        if len(h1) < 8:
            return [], -1
        key = h1[-1].time
        cached = self._line_cache.get((cfg.timeframe, key))
        if cached is not None:
            return cached
        highs = ind.highs(h1)
        lows = ind.lows(h1)
        atr_vals = ind.atr(h1, 14)
        swings = ind.fractal_swings(highs, lows, atr_vals, cfg.swing_floor_atr)
        swing_highs, swing_lows = ind.split_swings(swings)
        lines = ind.chain_trendlines(
            highs, lows, swing_highs, swing_lows, atr_vals,
            tolerance_atr=cfg.tolerance_atr,
            min_touches=cfg.min_touches,
        )
        result = (lines, len(h1) - 1)
        self._line_cache[(cfg.timeframe, key)] = result
        return result

    def _bias_as_of(self, t: datetime, timeframe: str) -> str | None:
        """BOS-based bias of an optional bias timeframe: 'long'/'short'/None."""
        cfg = self.config.trendline
        if cfg is None or self._provider is None:
            return None
        rows = self._provider(self.config.symbol, timeframe, cfg.bars)
        candles = [c for c in rows if c.symbol == self.config.symbol and c.time <= _closed_by(timeframe, t)]
        if len(candles) < 20:
            return None
        closes = ind.closes(candles)
        highs = ind.highs(candles)
        lows = ind.lows(candles)
        atr_vals = ind.atr(candles, 14)
        swings = ind.fractal_swings(highs, lows, atr_vals, cfg.swing_floor_atr)
        swing_highs, swing_lows = ind.split_swings(swings)
        bos = ind.detect_bos(closes, swing_highs, swing_lows)
        if not bos:
            return None
        return "long" if bos[-1]["direction"] == "bull" else "short"

    def safety_value(self, t: datetime, direction: Direction) -> float | None:
        """Current value of the safety line at ``t`` for a position direction."""
        lines, last_idx = self._lines_as_of(t)
        if not lines or last_idx < 0:
            return None
        kind = "support" if direction == Direction.LONG else "resistance"
        line = _last_line(lines, kind)
        if line is None:
            return None
        return line["value_at"](last_idx)

    # ── signal generation ─────────────────────────────────────
    def generate_signals(self, candles: list[Candle]) -> list[Signal]:
        cfg = self.config.trendline
        if cfg is None or self._provider is None or not candles:
            return []
        slow = self.config.entry.slow_ema_period
        if len(candles) < slow + 2:
            return []
        crosses = ind.ema_cross_signal(
            ind.closes(candles),
            self.config.entry.fast_ema_period,
            slow,
        )
        atr_vals = ind.atr(candles, self.config.entry.atr_period)

        signals: list[Signal] = []
        for i in range(slow + 1, len(candles)):
            cross = crosses[i]
            if cross is None:
                continue
            signal = self._build_signal(cross, candles[i], atr_vals[i], cfg)
            if signal is not None:
                signals.append(signal)
        return signals

    def _build_signal(self, cross: str, candle: Candle, atr_val: float,
                      cfg: TrendlineConfig) -> Signal | None:
        lines, last_idx = self._lines_as_of(candle.time)
        if not lines or last_idx < 0:
            return None
        if not np.isfinite(atr_val) or atr_val <= 0:
            atr_val = 1.0
        atr_val = float(atr_val)

        for tf in cfg.bias_timeframes:
            bias = self._bias_as_of(candle.time, tf)
            if (cross == "up" and bias == "short") or (cross == "down" and bias == "long"):
                return None

        direction = Direction.LONG if cross == "up" else Direction.SHORT
        action = _last_line(lines, "resistance" if direction == Direction.LONG else "support")
        if action is None:
            return None
        action_val = action["value_at"](last_idx)

        if direction == Direction.LONG:
            if candle.close <= action_val:
                return None
        elif candle.close >= action_val:
            return None

        safety = _last_line(lines, "support" if direction == Direction.LONG else "resistance")
        if safety is None:
            return None
        safety_val = safety["value_at"](last_idx)
        buffer = cfg.sl_buffer_atr * atr_val
        entry = candle.close
        if direction == Direction.LONG:
            if safety_val >= entry:
                return None
            sl = safety_val - buffer
        else:
            if safety_val <= entry:
                return None
            sl = safety_val + buffer
        if (direction == Direction.LONG and sl >= entry) or (direction == Direction.SHORT and sl <= entry):
            return None

        meta = {
            "candle_time": candle.time.isoformat(),
            "ema_cross": cross,
            "action_line": _line_meta(action, action_val, last_idx),
            "safety_line": _line_meta(safety, safety_val, last_idx),
            "atr_m5": round(atr_val, 4),
            "sl_buffer_atr": round(buffer, 4),
            "session": _session_name(self.sessions, candle.time) or "outside",
        }
        return Signal(
            provider=SignalSource.INTERNAL_STRATEGY,
            strategy_id=self.config.id,
            symbol=self.config.symbol,
            timeframe=self.config.timeframe,
            direction=direction,
            price=round(entry, 2),
            sl=round(sl, 2),
            tp=None,
            meta=meta,
            created_at=datetime.now(UTC),
        )


def _line_meta(line: dict, value: float, last_idx: int) -> dict[str, Any]:
    return {
        "type": line["type"],
        "value": round(value, 2),
        "value_at": round(line["value_at"](last_idx), 2),
        "slope": round(line["slope"], 6),
        "touches": line["touches"],
        "idx1": line["idx1"],
        "idx2": line["idx2"],
    }
