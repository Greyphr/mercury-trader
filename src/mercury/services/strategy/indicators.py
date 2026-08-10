"""Technical indicator implementations (vectorized with numpy).

Also hosts the Smart Money Concepts (SMC / ICT) primitives used by the
ICT strategy: fractal swings, Break-of-Structure (BOS), order blocks with
displacement + FVG, liquidity pooling, and auto-detected trendlines.
"""

from __future__ import annotations

import numpy as np

from mercury.core.validation import Candle


def closes(candles: list[Candle]) -> np.ndarray:
    return np.array([c.close for c in candles], dtype=float)


def highs(candles: list[Candle]) -> np.ndarray:
    return np.array([c.high for c in candles], dtype=float)


def lows(candles: list[Candle]) -> np.ndarray:
    return np.array([c.low for c in candles], dtype=float)


def sma(values: np.ndarray, period: int) -> np.ndarray:
    if period <= 0 or len(values) == 0:
        return np.full(len(values), np.nan)
    out = np.full(len(values), np.nan)
    cumsum = np.cumsum(np.nan_to_num(values))
    cumsum_count = np.cumsum(~np.isnan(values)).astype(float)
    out[period - 1 :] = (cumsum[period - 1 :] - np.concatenate([[0], cumsum[:-period]])) / (
        cumsum_count[period - 1 :] - np.concatenate([[0], cumsum_count[:-period]])
    )
    return out


def ema(values: np.ndarray, period: int) -> np.ndarray:
    out = np.full(len(values), np.nan)
    if len(values) < period:
        return out
    alpha = 2.0 / (period + 1)
    out[period - 1] = float(np.mean(values[:period]))
    for i in range(period, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def ema_cross_signal(closes: np.ndarray, fast_period: int, slow_period: int) -> list[str | None]:
    """EMA cross direction per candle index: ``'up'`` / ``'down'`` / ``None``.

    A candle is ``'up'`` when the fast EMA crosses above the slow EMA on that
    candle, ``'down'`` when it crosses below; candles during warmup or with no
    cross return ``None``.
    """
    fast = ema(closes, fast_period)
    slow = ema(closes, slow_period)
    out: list[str | None] = [None] * len(closes)
    for i in range(1, len(closes)):
        if np.isnan(fast[i]) or np.isnan(slow[i]) or np.isnan(fast[i - 1]) or np.isnan(slow[i - 1]):
            continue
        if fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]:
            out[i] = "up"
        elif fast[i - 1] >= slow[i - 1] and fast[i] < slow[i]:
            out[i] = "down"
    return out


def rsi(values: np.ndarray, period: int = 14) -> np.ndarray:
    out = np.full(len(values), np.nan)
    if len(values) < period + 1:
        return out
    deltas = np.diff(values)
    gains = np.clip(deltas, 0, None)
    losses = np.clip(-deltas, 0, None)
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            out[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i + 1] = 100.0 - (100.0 / (1.0 + rs))
    return out


def atr(candles: list[Candle], period: int = 14) -> np.ndarray:
    """Average True Range (Wilder). Returns array aligned to candle index."""
    n = len(candles)
    out = np.full(n, np.nan)
    if n < 2:
        return out
    tr = np.empty(n)
    tr[0] = candles[0].high - candles[0].low
    for i in range(1, n):
        prev_close = candles[i - 1].close
        tr[i] = max(
            candles[i].high - candles[i].low,
            abs(candles[i].high - prev_close),
            abs(candles[i].low - prev_close),
        )
    out[period - 1] = float(np.mean(tr[:period]))
    for i in range(period, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


# ──────────────────────────────────────────────────────────────
# SMC / ICT primitives
# ──────────────────────────────────────────────────────────────
def fractal_swings(
    highs: np.ndarray, lows: np.ndarray, atr: np.ndarray, floor_mult: float
) -> list[dict]:
    """Confirmed 5-candle fractal swings (2 candles each side).

    A swing is discarded when its height is below ``floor_mult`` x ATR(14)
    (filters micro-fractals). Returns ``[{type, idx, level}]`` in index order.
    """
    n = len(highs)
    out: list[dict] = []
    for i in range(2, n - 2):
        h = highs[i]
        if h > highs[i - 2] and h > highs[i - 1] and h > highs[i + 1] and h > highs[i + 2]:
            base = float(np.max(lows[i - 2 : i + 3]))
            if not np.isnan(atr[i]) and (h - base) >= floor_mult * atr[i]:
                out.append({"type": "high", "idx": i, "level": float(h)})
        low = lows[i]
        if low < lows[i - 2] and low < lows[i - 1] and low < lows[i + 1] and low < lows[i + 2]:
            base = float(np.min(highs[i - 2 : i + 3]))
            if not np.isnan(atr[i]) and (base - low) >= floor_mult * atr[i]:
                out.append({"type": "low", "idx": i, "level": float(low)})
    return out


def split_swings(swings: list[dict]) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Split fractal swings into ``(swing_highs, swing_lows)`` as ``(idx, level)``."""
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    for s in swings:
        if s["type"] == "high":
            highs.append((s["idx"], s["level"]))
        else:
            lows.append((s["idx"], s["level"]))
    return highs, lows


def detect_bos(closes: np.ndarray, swing_highs, swing_lows) -> list[dict]:
    """Break-of-Structure events (body close beyond the last confirmed swing).

    Returns ``[{direction: 'bull'|'bear', idx, level}]``. The reference swing
    is the running maximum confirmed swing high (bullish) / minimum confirmed
    swing low (bearish); after a break the reference floor moves to the close
    so a single level is only broken once.
    """
    events: list[dict] = []
    hp = lp = 0
    ref_high: float | None = None
    ref_low: float | None = None
    n_high, n_low = len(swing_highs), len(swing_lows)
    for i in range(len(closes)):
        while hp < n_high and swing_highs[hp][0] <= i - 2:
            ref_high = swing_highs[hp][1] if ref_high is None else max(ref_high, swing_highs[hp][1])
            hp += 1
        while lp < n_low and swing_lows[lp][0] <= i - 2:
            ref_low = swing_lows[lp][1] if ref_low is None else min(ref_low, swing_lows[lp][1])
            lp += 1
        if ref_high is not None and closes[i] > ref_high:
            events.append({"direction": "bull", "idx": i, "level": float(ref_high)})
            ref_high = closes[i]
        if ref_low is not None and closes[i] < ref_low:
            events.append({"direction": "bear", "idx": i, "level": float(ref_low)})
            ref_low = closes[i]
    return events


def cluster_levels(levels: list[float], tolerance: float) -> list[list[float]]:
    """Greedily group price levels within ``tolerance`` of each other."""
    if not levels:
        return []
    clusters: list[list[float]] = []
    for lvl in sorted(levels):
        if clusters and abs(lvl - clusters[-1][0]) <= tolerance:
            clusters[-1].append(lvl)
        else:
            clusters.append([lvl])
    return clusters


def detect_order_blocks(
    opens: np.ndarray,
    closes: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    *,
    period: int,
    body_mult: float,
    ob_lookback: int,
) -> list[dict]:
    """Order blocks: last opposite-colored candle before a displacement that
    leaves a 3-candle FVG. Returns ``[{direction, idx, lo, hi, disp_idx}]``
    where ``direction`` is the block's trade direction ('bull' = buy block).
    """
    n = len(closes)
    bodies = np.abs(closes - opens)
    body_sums = np.concatenate([[0.0], np.cumsum(bodies)])
    obs: list[dict] = []
    for d in range(period, n):
        avg = (body_sums[d] - body_sums[d - period]) / period
        if avg <= 0 or bodies[d] < body_mult * avg or d < 2:
            continue
        bull = closes[d] > opens[d]
        if bull and not (lows[d] > highs[d - 2]):
            continue
        if not bull and not (highs[d] < lows[d - 2]):
            continue
        ob_idx: int | None = None
        for j in range(d - 1, max(d - 1 - ob_lookback, -1), -1):
            if bull and closes[j] < opens[j]:
                ob_idx = j
                break
            if not bull and closes[j] > opens[j]:
                ob_idx = j
                break
        if ob_idx is None:
            continue
        obs.append(
            {
                "direction": "bull" if bull else "bear",
                "idx": ob_idx,
                "lo": float(min(opens[ob_idx], closes[ob_idx])),
                "hi": float(max(opens[ob_idx], closes[ob_idx])),
                "disp_idx": d,
            }
        )
    return obs


def ob_mitigated(closes: np.ndarray, ob: dict, after_idx: int) -> bool:
    """True when a body has closed beyond the OB's far edge (full range covered).

    For a bull (buy) block the far edge is the block low; for a bear (sell)
    block it is the block high.
    """
    if ob["direction"] == "bull":
        return any(closes[after_idx:] < ob["lo"])
    return any(closes[after_idx:] > ob["hi"])


def detect_trendlines(
    highs: np.ndarray,
    lows: np.ndarray,
    swing_highs: list[tuple[int, float]],
    swing_lows: list[tuple[int, float]],
    atr: np.ndarray,
    *,
    min_touches: int,
    tolerance_atr: float,
    max_swings: int,
    recent_bars: int,
) -> list[dict]:
    """Auto-detect the best H1 support and resistance trendlines.

    A line through two swing points is valid when it is touched (wick within
    ``tolerance_atr`` x ATR) at least ``min_touches`` times and its last touch
    is within the last ``recent_bars`` candles. Support lines must ascend,
    resistance lines must descend. Returns ``[{type, idx1, idx2, slope,
    touches, last_touch, value_at(idx)}]``.
    """
    n = len(highs)
    if n < 8:
        return []
    mean_atr = float(np.nanmean(atr)) if np.any(np.isfinite(atr)) else 1.0
    tol = tolerance_atr * mean_atr
    lines: list[dict] = []

    def _build_points(swings: list[tuple[int, float]]) -> list[tuple[int, float]]:
        return swings[-max_swings:] if len(swings) > max_swings else swings

    def _value_at(a_idx: int, a_lvl: float, slope: float, k: int) -> float:
        return a_lvl + slope * (k - a_idx)

    for kind, points, is_support in (
        ("support", _build_points(swing_lows), True),
        ("resistance", _build_points(swing_highs), False),
    ):
        best: dict | None = None
        m = len(points)
        for i in range(m):
            for j in range(i + 1, m):
                a_idx, a_lvl = points[i]
                b_idx, b_lvl = points[j]
                if b_idx == a_idx:
                    continue
                slope = (b_lvl - a_lvl) / (b_idx - a_idx)
                if is_support and slope <= 0:
                    continue
                if not is_support and slope >= 0:
                    continue
                touches = 0
                last_touch = -1
                for k in range(a_idx, b_idx + 1):
                    v = _value_at(a_idx, a_lvl, slope, k)
                    ref = lows[k] if is_support else highs[k]
                    if abs(ref - v) <= tol:
                        touches += 1
                        last_touch = k
                if touches < min_touches:
                    continue
                if last_touch < n - recent_bars:
                    continue
                score = touches * 1000 + last_touch
                if best is None or score > best["_score"]:
                    best = {
                        "type": kind,
                        "idx1": a_idx,
                        "idx2": b_idx,
                        "slope": slope,
                        "touches": touches,
                        "last_touch": last_touch,
                        "value_at": lambda k, a=a_idx, lv=a_lvl, s=slope: _value_at(a, lv, s, k),
                    }
                    best["_score"] = score
        if best is not None:
            best.pop("_score", None)
            lines.append(best)
    return lines


def chain_trendlines(
    highs: np.ndarray,
    lows: np.ndarray,
    swing_highs: list[tuple[int, float]],
    swing_lows: list[tuple[int, float]],
    atr: np.ndarray,
    *,
    tolerance_atr: float,
    min_touches: int,
) -> list[dict]:
    """Chained trendlines built from consecutive swing points of one polarity.

    A segment between two consecutive swing points is valid when its slope
    points in the trend direction (support segments ascend, resistance
    segments descend), price never violates it by more than ``tolerance_atr``
    x ATR between the anchors (support: no low below line - tol; resistance:
    no high above line + tol), and it is touched (wick within tolerance) at
    least ``min_touches`` times. Adjacent valid segments chain end-to-end —
    the previous end becomes the next start — so consecutive uptrend lows form
    one continuous support line and consecutive downtrend highs one resistance
    line.

    Returns every valid segment in ``detect_trendlines`` dict shape
    ``{type, idx1, idx2, slope, touches, last_touch, value_at(idx)}``; the
    last entry of each type is the most recently established (currently
    active) line.
    """
    n = len(highs)
    if n < 8:
        return []
    mean_atr = float(np.nanmean(atr)) if np.any(np.isfinite(atr)) else 1.0
    tol = tolerance_atr * mean_atr

    def _value_at(a_idx: int, a_lvl: float, slope: float, k: int) -> float:
        return a_lvl + slope * (k - a_idx)

    lines: list[dict] = []
    for kind, points, is_support in (
        ("support", swing_lows, True),
        ("resistance", swing_highs, False),
    ):
        for i in range(len(points) - 1):
            a_idx, a_lvl = points[i]
            b_idx, b_lvl = points[i + 1]
            if b_idx <= a_idx:
                continue
            slope = (b_lvl - a_lvl) / (b_idx - a_idx)
            if is_support and slope <= 0:
                continue
            if not is_support and slope >= 0:
                continue
            touches = 0
            last_touch = -1
            valid = True
            for k in range(a_idx, b_idx + 1):
                v = _value_at(a_idx, a_lvl, slope, k)
                ref = lows[k] if is_support else highs[k]
                if is_support and ref < v - tol:
                    valid = False
                    break
                if not is_support and ref > v + tol:
                    valid = False
                    break
                if abs(ref - v) <= tol:
                    touches += 1
                    last_touch = k
            if not valid or touches < min_touches:
                continue
            lines.append(
                {
                    "type": kind,
                    "idx1": a_idx,
                    "idx2": b_idx,
                    "slope": slope,
                    "touches": touches,
                    "last_touch": last_touch,
                    "value_at": lambda k, a=a_idx, lv=a_lvl, s=slope: _value_at(a, lv, s, k),
                }
            )
    return lines


def detect_opposite_bos(candles: list[Candle], direction: str, *, floor_atr_mult: float = 0.25) -> int | None:
    """Index of the first opposite-direction BOS in ``candles``, or None.

    For a long position this is a bearish BOS (body closes below the running
    minimum confirmed swing low); mirrored for shorts. Used for the early-exit
    management rule (exit when an opposite BOS forms before TP/SL).
    """
    if len(candles) < 10:
        return None
    highs = np.array([c.high for c in candles], dtype=float)
    lows = np.array([c.low for c in candles], dtype=float)
    closes = np.array([c.close for c in candles], dtype=float)
    atr_vals = atr(candles, 14)
    swings = fractal_swings(highs, lows, atr_vals, floor_atr_mult)
    swing_highs, swing_lows = split_swings(swings)

    ref: float | None = None
    hp = lp = 0
    n_high, n_low = len(swing_highs), len(swing_lows)
    for i in range(len(closes)):
        if direction == "long":
            while lp < n_low and swing_lows[lp][0] <= i - 2:
                ref = swing_lows[lp][1] if ref is None else min(ref, swing_lows[lp][1])
                lp += 1
            if ref is not None and closes[i] < ref:
                return i
        else:
            while hp < n_high and swing_highs[hp][0] <= i - 2:
                ref = swing_highs[hp][1] if ref is None else max(ref, swing_highs[hp][1])
                hp += 1
            if ref is not None and closes[i] > ref:
                return i
    return None
