"""ICT / Smart Money Concepts strategy (Trading Strategy Specification V1).

Replicates the discretionary SMC playbook mechanically and time-consistently:

* HTF context: H1 bias = direction of the last H1 BOS; H4 filter must not
  oppose it (and ``htf_alignment`` requires H4 bias to point the same way).
* Sweep (mandatory): an M5 wick must trade beyond an H1 liquidity pool
  (prior swing / equal high-low, excluding round numbers and session
  extremes) and close back inside.
* Plus one: an unmitigated H1 order block in the path of price, an
  auto-detected H1 trendline bounce, or H1+H4 alignment.
* Entry: M5 confirmation-close beyond the structural far edge (CE — the
  near edge — is only the trigger). No blind limit entries.
* SL beyond the block + 0.5 x M5 ATR(14); BE at 1R; TP = next opposing M5
  liquidity (skip when < 2R, no cap, no partials); early exit on an
  opposite M5 BOS; max one re-entry per level.

Only confirmed (closed) candles are used, and every M5 evaluation only
sees H1/H4 candles that have fully closed by that point in time.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from mercury.core.config import ICTConfig, TradingSession, session_allows
from mercury.core.validation import Candle
from mercury.models.schemas import Direction, Signal, SignalSource
from mercury.services.strategy import indicators as ind
from mercury.services.strategy.strategies import Strategy

ContextProvider = Callable[[str, str, int], list[Candle]]


def _session_name(sessions: list[TradingSession], dt: datetime) -> str | None:
    """Name of the session that contains ``dt`` (mirrors ``session_allows``)."""
    weekday = dt.strftime("%a").lower()[:3]
    t = dt.strftime("%H:%M")
    for session in sessions:
        if weekday not in session.days:
            continue
        if session.start <= t <= session.end:
            pause = session.pause
            if pause is not None:
                start = pause.get("start")
                end = pause.get("end")
                if start is not None and end is not None and start <= t <= end:
                    continue
            return session.name
    return None


# ──────────────────────────────────────────────────────────────
# HTF snapshots
# ──────────────────────────────────────────────────────────────
@dataclass
class H1Snapshot:
    h1_len: int
    atr_mean: float
    swing_highs: list[tuple[int, float]]
    swing_lows: list[tuple[int, float]]
    bos: list[dict]
    bias: str | None
    buy_blocks: list[dict]      # unmitigated bull (buy) order blocks
    sell_blocks: list[dict]     # unmitigated bear (sell) order blocks
    sell_liquidity: list[dict]  # {"type","level"} — pools below price
    buy_liquidity: list[dict]   # pools above price
    trendline_support: dict | None
    trendline_resistance: dict | None


@dataclass
class H4Snapshot:
    bias: str | None


def _nanmean(values: np.ndarray) -> float:
    if np.any(np.isfinite(values)):
        return float(np.nanmean(values))
    return 1.0


def _excluded(level: float, day: Any, side: str, ctx: Any, day_high: float | None, day_low: float | None) -> bool:
    """Round-number and session high/low exclusions for liquidity levels."""
    if ctx.exclude_session_high_low:
        if side == "buy" and day_high is not None and abs(level - day_high) <= ctx.round_number_tol:
            return True
        if side == "sell" and day_low is not None and abs(level - day_low) <= ctx.round_number_tol:
            return True
    if ctx.round_number_step > 0:
        nearest = round(level / ctx.round_number_step) * ctx.round_number_step
        if abs(level - nearest) <= ctx.round_number_tol:
            return True
    return False


def _day_extremes(candles: list[Candle]) -> tuple[dict, dict]:
    day_highs: dict[Any, float] = {}
    day_lows: dict[Any, float] = {}
    for c in candles:
        d = c.time.date()
        day_highs[d] = max(day_highs.get(d, float("-inf")), c.high)
        day_lows[d] = min(day_lows.get(d, float("inf")), c.low)
    return day_highs, day_lows


def _liquidity_levels(
    candles: list[Candle], swings: list[tuple[int, float]], side: str, tol: float, ctx: Any
) -> list[dict]:
    day_highs, day_lows = _day_extremes(candles)
    levels: list[dict] = []
    for idx, lvl in swings:
        day = candles[idx].time.date()
        dh = day_highs.get(day)
        dl = day_lows.get(day)
        if _excluded(lvl, day, side, ctx, dh, dl):
            continue
        levels.append({"type": "swing", "level": lvl})
    if len(swings) >= 2:
        for cluster in ind.cluster_levels([lvl for _, lvl in swings], tol):
            if len(cluster) >= 2:
                levels.append({"type": "equal", "level": sum(cluster) / len(cluster)})
    return levels


def build_h1_snapshot(h1: list[Candle], ict: ICTConfig) -> H1Snapshot | None:
    if len(h1) < 40:
        return None
    opens = np.array([c.open for c in h1], dtype=float)
    closes = np.array([c.close for c in h1], dtype=float)
    highs = np.array([c.high for c in h1], dtype=float)
    lows = np.array([c.low for c in h1], dtype=float)
    atr_vals = ind.atr(h1, 14)
    atr_mean = _nanmean(atr_vals)

    swings = ind.fractal_swings(highs, lows, atr_vals, ict.context.swing_floor_atr)
    swing_highs, swing_lows = ind.split_swings(swings)
    bos = ind.detect_bos(closes, swing_highs, swing_lows)
    bias = "long" if bos and bos[-1]["direction"] == "bull" else ("short" if bos and bos[-1]["direction"] == "bear" else None)

    obs = ind.detect_order_blocks(
        opens, closes, highs, lows,
        period=ict.displacement.period,
        body_mult=ict.displacement.body_mult,
        ob_lookback=ict.displacement.ob_lookback,
    )
    buy_blocks: list[dict] = []
    sell_blocks: list[dict] = []
    for ob in obs:
        if ind.ob_mitigated(closes, ob, ob["idx"] + 1):
            continue
        (buy_blocks if ob["direction"] == "bull" else sell_blocks).append(ob)

    tol = ict.context.eq_tolerance_atr * atr_mean
    ctx = ict.context
    sell_liquidity = _liquidity_levels(h1, swing_lows, "sell", tol, ctx)
    buy_liquidity = _liquidity_levels(h1, swing_highs, "buy", tol, ctx)

    lines = ind.detect_trendlines(
        highs, lows, swing_highs, swing_lows, atr_vals,
        min_touches=ict.trendline.min_touches,
        tolerance_atr=ict.trendline.tolerance_atr,
        max_swings=ict.trendline.max_swings,
        recent_bars=ict.trendline.recent_bars,
    )
    support = next((line for line in lines if line["type"] == "support"), None)
    resistance = next((line for line in lines if line["type"] == "resistance"), None)

    return H1Snapshot(
        h1_len=len(h1),
        atr_mean=atr_mean,
        swing_highs=swing_highs,
        swing_lows=swing_lows,
        bos=bos,
        bias=bias,
        buy_blocks=buy_blocks,
        sell_blocks=sell_blocks,
        sell_liquidity=sell_liquidity,
        buy_liquidity=buy_liquidity,
        trendline_support=support,
        trendline_resistance=resistance,
    )


def build_h4_snapshot(h4: list[Candle], ict: ICTConfig) -> H4Snapshot | None:
    if len(h4) < 20:
        return None
    closes = np.array([c.close for c in h4], dtype=float)
    highs = np.array([c.high for c in h4], dtype=float)
    lows = np.array([c.low for c in h4], dtype=float)
    atr_vals = ind.atr(h4, 14)
    swings = ind.fractal_swings(highs, lows, atr_vals, ict.context.swing_floor_atr)
    swing_highs, swing_lows = ind.split_swings(swings)
    bos = ind.detect_bos(closes, swing_highs, swing_lows)
    bias = "long" if bos and bos[-1]["direction"] == "bull" else ("short" if bos and bos[-1]["direction"] == "bear" else None)
    return H4Snapshot(bias=bias)


# ──────────────────────────────────────────────────────────────
# M5 state machine
# ──────────────────────────────────────────────────────────────
@dataclass
class _Setup:
    level: float
    sweep_idx: int
    kind: str | None = None          # 'swing' | 'equal'
    sweep_time: datetime | None = None
    active: bool = True
    confirmed: bool = False
    structural: float | None = None  # far edge used for SL / invalidation


class M5Tracker:
    """Walk-forward M5 structure + entry state machine (time-consistent)."""

    def __init__(self, ict: ICTConfig) -> None:
        self.ict = ict
        self.idx = -1
        self._window: deque = deque(maxlen=5)
        self._day: Any = None
        self._day_high = float("-inf")
        self._day_low = float("inf")
        self.tp_buy_levels: list[tuple[int, float]] = []    # (idx, level) M5 liquidity above (long TP)
        self.tp_sell_levels: list[tuple[int, float]] = []   # (idx, level) M5 liquidity below (short TP)
        self.long_setup: _Setup | None = None
        self.short_setup: _Setup | None = None
        self._attempts: dict[tuple[str, float], int] = {}

    # ── per-candle advance ────────────────────────────────────
    def advance(self, candle: Candle, atr_val: float, h1: H1Snapshot | None,
                h4: H4Snapshot | None, sessions: list[TradingSession],
                build_signal: Callable[[str, _Setup, dict, Candle, float], Signal | None]) -> None:
        self.idx += 1
        self._update_structure(candle, atr_val)

        if not session_allows(sessions, candle.time):
            return
        if h1 is None or h4 is None:
            return

        self._expire_setups()
        self._check_sweep(candle, h1)
        self._check_confirmation(candle, atr_val, h1, h4, build_signal)
        self._check_invalidation(candle)

    def _update_structure(self, candle: Candle, atr_val: float) -> None:
        day = candle.time.date()
        if day != self._day:
            self._day = day
            self._day_high = candle.high
            self._day_low = candle.low
        else:
            self._day_high = max(self._day_high, candle.high)
            self._day_low = min(self._day_low, candle.low)

        self._window.append(candle)
        if len(self._window) != 5:
            return
        mid = self._window[2]
        others = [self._window[i] for i in range(5) if i != 2]

        ctx = self.ict.context
        if mid.high > max(o.high for o in others):
            if not _excluded(mid.high, day, "buy", ctx, self._day_high, self._day_low):
                self.tp_buy_levels.append((self.idx, mid.high))
                if len(self.tp_buy_levels) >= 2 and abs(
                    self.tp_buy_levels[-1][1] - self.tp_buy_levels[-2][1]
                ) <= ctx.eq_tolerance_atr * atr_val:
                    self.tp_buy_levels.append((self.idx, max(self.tp_buy_levels[-1][1], self.tp_buy_levels[-2][1])))
        if mid.low < min(o.low for o in others):
            if not _excluded(mid.low, day, "sell", ctx, self._day_high, self._day_low):
                self.tp_sell_levels.append((self.idx, mid.low))
                if len(self.tp_sell_levels) >= 2 and abs(
                    self.tp_sell_levels[-1][1] - self.tp_sell_levels[-2][1]
                ) <= ctx.eq_tolerance_atr * atr_val:
                    self.tp_sell_levels.append((self.idx, min(self.tp_sell_levels[-1][1], self.tp_sell_levels[-2][1])))

    def _can_attempt(self, direction: str, level: float) -> bool:
        key = (direction, round(level, 2))
        return self._attempts.get(key, 0) < self.ict.reentry.max_attempts_per_level

    def _consume_attempt(self, direction: str, level: float) -> None:
        key = (direction, round(level, 2))
        self._attempts[key] = self._attempts.get(key, 0) + 1

    def _expire_setups(self) -> None:
        fresh = self.ict.sweep.fresh_bars
        for direction, setup in (("long", self.long_setup), ("short", self.short_setup)):
            if setup is not None and setup.active and self.idx - setup.sweep_idx > fresh:
                setup.active = False
                self._consume_attempt(direction, setup.level)

    def _check_sweep(self, candle: Candle, h1: H1Snapshot) -> None:
        ict = self.ict
        max_dist = ict.sweep.max_distance_points / 100.0

        if not (self.long_setup is not None and self.long_setup.active):
            swept = [
                lv for lv in h1.sell_liquidity
                if candle.low < lv["level"] < candle.close and lv["level"] >= candle.close - max_dist
            ]
            if swept:
                level = max(lv["level"] for lv in swept)
                if self._can_attempt("long", level):
                    self.long_setup = _Setup(level=level, sweep_idx=self.idx,
                                             kind=next(lv["type"] for lv in swept if lv["level"] == level),
                                             sweep_time=candle.time)

        if not (self.short_setup is not None and self.short_setup.active):
            swept = [
                lv for lv in h1.buy_liquidity
                if candle.high > lv["level"] > candle.close and lv["level"] <= candle.close + max_dist
            ]
            if swept:
                level = min(lv["level"] for lv in swept)
                if self._can_attempt("short", level):
                    self.short_setup = _Setup(level=level, sweep_idx=self.idx,
                                              kind=next(lv["type"] for lv in swept if lv["level"] == level),
                                              sweep_time=candle.time)

    def _check_confirmation(self, candle: Candle, atr_val: float, h1: H1Snapshot,
                            h4: H4Snapshot, build_signal: Callable) -> None:
        ict = self.ict
        for direction, setup in (("long", self.long_setup), ("short", self.short_setup)):
            if setup is None or not setup.active or setup.confirmed:
                continue
            if self.idx - setup.sweep_idx > ict.confirmation.lookback_bars:
                continue
            if h4 is None:
                continue
            if direction == "long" and h4.bias == "short":
                continue
            if direction == "short" and h4.bias == "long":
                continue
            plus_one = self._select_plus_one(direction, setup, candle, atr_val, h1, h4, ict)
            if plus_one is None:
                continue
            kind, far_edge, structural = plus_one
            confirmed = candle.close > far_edge if direction == "long" else candle.close < far_edge
            if not confirmed:
                continue
            setup.confirmed = True
            setup.active = False
            setup.structural = structural
            self._consume_attempt(direction, setup.level)
            build_signal(direction, setup, {"kind": kind, "far_edge": far_edge}, candle, atr_val)

    def _select_plus_one(self, direction: str, setup: _Setup, candle: Candle, atr_val: float,
                         h1: H1Snapshot, h4: H4Snapshot, ict: ICTConfig) -> tuple[str, float, float] | None:
        zone = ict.sl_buffer_atr * atr_val
        if direction == "long":
            for ob in h1.buy_blocks:
                if ob["lo"] - zone - 0.5 * atr_val <= setup.level <= ob["hi"]:
                    return ("order_block", ob["hi"], ob["lo"])
            tl = h1.trendline_support
            if tl is not None:
                value = tl["value_at"](h1.h1_len - 1)
                if abs(value - setup.level) <= 1.5 * atr_val:
                    return ("trendline_bounce", value, value)
            if h1.bias == "long" and h4.bias == "long":
                return ("htf_alignment", setup.level, setup.level)
        else:
            for ob in h1.sell_blocks:
                if ob["lo"] <= setup.level <= ob["hi"] + zone + 0.5 * atr_val:
                    return ("order_block", ob["lo"], ob["hi"])
            tl = h1.trendline_resistance
            if tl is not None:
                value = tl["value_at"](h1.h1_len - 1)
                if abs(value - setup.level) <= 1.5 * atr_val:
                    return ("trendline_bounce", value, value)
            if h1.bias == "short" and h4.bias == "short":
                return ("htf_alignment", setup.level, setup.level)
        return None

    def _check_invalidation(self, candle: Candle) -> None:
        for direction, setup in (("long", self.long_setup), ("short", self.short_setup)):
            if setup is None or not setup.active or setup.confirmed:
                continue
            broke = candle.close < setup.level if direction == "long" else candle.close > setup.level
            if broke:
                setup.active = False
                self._consume_attempt(direction, setup.level)


# ──────────────────────────────────────────────────────────────
# ICT strategy
# ──────────────────────────────────────────────────────────────
class ICTStrategy(Strategy):
    """H1 bias + H4 filter + H1 liquidity sweep + OB/trendline/alignment
    plus-one + M5 confirmation-close entry."""

    def __init__(self, config, *, context_provider: ContextProvider | None = None,
                 settings: Any | None = None) -> None:
        super().__init__(config)
        self._provider = context_provider
        self._settings = settings
        self._signals: list[Signal] = []
        self._h1_snapshots: dict[datetime, H1Snapshot] = {}

    @property
    def sessions(self) -> list[TradingSession]:
        if self._settings is not None:
            return self._settings.base.trading_sessions
        return []

    def set_context_provider(self, provider: ContextProvider) -> None:
        self._provider = provider

    def generate_signals(self, candles: list[Candle]) -> list[Signal]:
        ict = self.config.ict
        if ict is None:
            raise ValueError(
                f"ICTStrategy '{self.config.id}' is missing the required 'ict' config block"
            )
        if self._provider is None or not candles:
            return []
        h1 = self._provider(self.config.symbol, "H1", ict.context.h1_bars)
        h4 = self._provider(self.config.symbol, "H4", ict.context.h4_bars)
        h1 = [c for c in h1 if c.symbol == self.config.symbol]
        if len(candles) < 100 or len(h1) < 40 or len(h4) < 20:
            return []
        return self._run(candles, h1, h4, ict)

    def _run(self, m5: list[Candle], h1: list[Candle], h4: list[Candle],
             ict: ICTConfig) -> list[Signal]:
        self._signals = []
        tracker = M5Tracker(ict)
        atr_m5 = ind.atr(m5, 14)
        h1_times = [c.time for c in h1]
        h4_times = [c.time for c in h4]

        h1_snap: H1Snapshot | None = None
        h4_snap: H4Snapshot | None = None
        prev_h1 = -1
        prev_h4 = -1
        used_h1_keys: set[datetime] = set()

        def build_signal(direction: str, setup: _Setup, plus_one: dict, candle: Candle,
                         atr_val: float) -> Signal | None:
            sig = self._build_signal(ict, direction, setup, plus_one, candle, atr_val, h1_snap, h4_snap, tracker)
            if sig is not None:
                self._signals.append(sig)
            return sig

        for i, candle in enumerate(m5):
            t = candle.time
            hi = bisect_right(h1_times, t - timedelta(hours=1))
            h4i = bisect_right(h4_times, t - timedelta(hours=4))
            if hi != prev_h1:
                h1_snap = self._h1_snapshot(h1[:hi], ict)
                if h1_snap is not None:
                    used_h1_keys.add(h1[hi - 1].time)
                prev_h1 = hi
            if h4i != prev_h4:
                h4_snap = build_h4_snapshot(h4[:h4i], ict)
                prev_h4 = h4i
            if h1_snap is None or h4_snap is None:
                continue
            tracker.advance(candle, atr_m5[i], h1_snap, h4_snap, self.sessions, build_signal)
        self._h1_snapshots = {k: v for k, v in self._h1_snapshots.items() if k in used_h1_keys}
        return self._signals

    def _h1_snapshot(self, prefix: list[Candle], ict: ICTConfig) -> H1Snapshot | None:
        """Build (or reuse) the H1 snapshot for a prefix of closed H1 candles.

        The snapshot — fractal swings, BOS, order blocks, liquidity, and
        ``detect_trendlines`` — is a pure function of the H1 prefix, which only
        changes once per H1 candle close. Memoizing by the prefix's last candle
        time means the M5 replay on every ``market.data.updated`` tick (60s)
        reuses prior work instead of recomputing on unchanged H1 data.
        """
        if not prefix:
            return None
        key = prefix[-1].time
        cached = self._h1_snapshots.get(key)
        if cached is None:
            cached = build_h1_snapshot(prefix, ict)
            if cached is not None:
                self._h1_snapshots[key] = cached
        return cached

    def _build_signal(self, ict: ICTConfig, direction: str, setup: _Setup, plus_one: dict,
                      candle: Candle, atr_val: float, h1: H1Snapshot | None,
                      h4: H4Snapshot | None, tracker: M5Tracker) -> Signal | None:
        atr_val = float(atr_val) if np.isfinite(atr_val) else 1.0
        sl_buffer = ict.sl_buffer_atr * atr_val
        entry = candle.close

        if direction == "long":
            if setup.structural is None or entry <= setup.structural:
                return None
            sl = setup.structural - sl_buffer
            candidates = [lvl for i, lvl in tracker.tp_buy_levels if i > setup.sweep_idx and lvl > entry]
            tp = min(candidates) if candidates else None
            if tp is None or sl >= entry:
                return None
        else:
            if setup.structural is None or entry >= setup.structural:
                return None
            sl = setup.structural + sl_buffer
            candidates = [lvl for i, lvl in tracker.tp_sell_levels if i > setup.sweep_idx and lvl < entry]
            tp = max(candidates) if candidates else None
            if tp is None or sl <= entry:
                return None

        risk = abs(entry - sl)
        rr = abs(tp - entry) / risk if risk > 0 else 0.0
        if rr < ict.min_rr:
            return None

        meta = {
            "candle_time": candle.time.isoformat(),
            "setup": plus_one["kind"],
            "sweep_level": round(setup.level, 2),
            "sweep_time": setup.sweep_time.isoformat() if setup.sweep_time else candle.time.isoformat(),
            "structural_level": round(setup.structural, 2),
            "bias_h1": h1.bias if h1 else None,
            "bias_h4": h4.bias if h4 else None,
            "atr_m5": round(atr_val, 4),
            "sl_buffer_atr": round(sl_buffer, 4),
            "rr": round(rr, 2),
            "session": _session_name(self.sessions, candle.time) or "outside",
        }
        return Signal(
            provider=SignalSource.INTERNAL_STRATEGY,
            strategy_id=self.config.id,
            symbol=self.config.symbol,
            timeframe=self.config.timeframe,
            direction=Direction.LONG if direction == "long" else Direction.SHORT,
            price=round(entry, 2),
            sl=round(sl, 2),
            tp=round(tp, 2),
            meta=meta,
            created_at=datetime.now(UTC),
        )
