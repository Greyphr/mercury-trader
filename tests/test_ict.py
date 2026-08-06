"""Tests for the ICT/SMC strategy (Spec V1).

Uses synthetic H1/H4/M5 candles engineered to produce one deterministic setup:
an H1 buy order block with displacement + FVG, a pullback swing low (H1
sell-side liquidity), an M5 wick sweep of that level, and an M5 confirmation
close above the block. Verifies signal geometry, session gating, snapshot
building, opposite-BOS detection, and the end-to-end backtest.
"""

from datetime import UTC, datetime, timedelta

from mercury.core.config import (
    ICTConfig,
    ICTConfirmationConfig,
    ICTContextConfig,
    ICTDisplacementConfig,
    ICTManagementConfig,
    ICTReentryConfig,
    ICTSweepConfig,
)
from mercury.core.validation import Candle
from mercury.services.backtest.engine import run_backtest
from mercury.services.strategy import indicators as ind
from mercury.services.strategy.ict import ICTStrategy, build_h1_snapshot

SYM = "XAUUSD"


def _candle(t, o, h, lo, c, timeframe="M5"):
    return Candle(symbol=SYM, timeframe=timeframe, time=t, open=o, high=h, low=lo, close=c, volume=100)


def _ict_config():
    return ICTConfig(
        context=ICTContextConfig(exclude_session_high_low=False),
        displacement=ICTDisplacementConfig(period=20, body_mult=1.5, ob_lookback=5),
        sweep=ICTSweepConfig(max_distance_points=300, fresh_bars=12),
        confirmation=ICTConfirmationConfig(lookback_bars=8),
        sl_buffer_atr=0.5,
        min_rr=1.2,
        management=ICTManagementConfig(breakeven_at_r=1.0, early_exit_on_opposite_bos=True),
        reentry=ICTReentryConfig(max_attempts_per_level=2),
    )


def _h1_candles():
    """80 H1 candles: baseline → bearish OB (32) → bull displacement (33)
    → pullback swing low at 49 (2398.7) → recovery."""
    series = []
    price = 2400.5
    for i in range(30):
        o = price
        c = price - 0.15 if i % 3 == 0 else price + 0.1
        series.append((o, max(o, c) + 0.35, min(o, c) - 0.25, c))
        price = c
    series.append((2399.3, 2399.6, 2398.5, 2398.8))   # 30
    series.append((2398.8, 2399.1, 2398.6, 2399.0))   # 31
    series.append((2399.0, 2399.2, 2398.7, 2398.7))   # 32 (OB: lo 2398.7, hi 2399.0)
    series.append((2399.2, 2401.6, 2399.2, 2401.2))   # 33 displacement + FVG (gap above 31)
    price = 2401.0
    for _ in range(12):                                # 34..45
        o = price
        c = price + 0.12
        series.append((o, c + 0.3, o - 0.2, c))
        price = c
    series.append((2402.2, 2402.5, 2401.5, 2401.9))   # 46
    series.append((2401.9, 2402.0, 2400.6, 2400.9))   # 47
    series.append((2400.9, 2401.1, 2399.6, 2399.9))   # 48
    series.append((2399.9, 2400.3, 2398.7, 2400.0))   # 49 swing low
    series.append((2400.0, 2400.5, 2399.6, 2400.3))   # 50
    series.append((2400.3, 2400.9, 2399.9, 2400.7))   # 51
    price = 2400.7
    for i in range(28):                                # 52..79
        o = price
        c = price + (0.1 if i % 2 else -0.05)
        series.append((o, max(o, c) + 0.35, min(o, c) - 0.25, c))
        price = c
    t0 = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
    return [_candle(t0 + timedelta(hours=i), *row, "H1") for i, row in enumerate(series)]


def _h4_candles():
    out = []
    t = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    price = 2395.0
    for _ in range(15):                                   # 0..14 monotonic rise
        out.append(_candle(t, price, price + 1.0, price - 0.7, price + 0.3, "H4"))
        price += 0.3
        t += timedelta(hours=4)
    for row in [                                          # 15..20 pullback + bull BOS
        (price, price + 2.0, price - 0.2, price + 0.4),   # 15 swing high
        (price + 0.4, price + 1.2, price - 0.3, price + 0.1),
        (price + 0.1, price + 1.0, price - 0.6, price + 0.0),
        (price + 0.0, price + 0.7, price - 0.7, price + 0.2),
        (price + 0.2, price + 1.2, price - 0.4, price + 0.6),
        (price + 0.6, price + 2.3, price + 0.5, price + 2.0),  # 20 breaks swing high
    ]:
        out.append(_candle(t, *row, "H4"))
        price = row[3]
        t += timedelta(hours=4)
    for _ in range(39):                                   # 21..59 monotonic rise
        out.append(_candle(t, price, price + 1.0, price - 0.7, price + 0.3, "H4"))
        price += 0.3
        t += timedelta(hours=4)
    return out


def _m5_candles():
    out = []
    t = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
    price = 2400.4
    for i in range(60):                                # calm prelude, no sweeps
        o = price
        c = o + (0.1 if i % 2 else -0.1)
        out.append(_candle(t, o, max(o, c) + 0.25, min(o, c) - 0.2, c))
        price = c
        t += timedelta(minutes=5)
    out.append(_candle(t, 2400.3, 2400.8, 2398.2, 2399.0))   # 60 sweep
    t += timedelta(minutes=5)
    out.append(_candle(t, 2399.0, 2400.2, 2398.7, 2398.9))   # 61
    t += timedelta(minutes=5)
    out.append(_candle(t, 2398.9, 2402.0, 2398.6, 2398.8))   # 62 M5 swing high 2402.0
    t += timedelta(minutes=5)
    out.append(_candle(t, 2398.8, 2399.5, 2398.5, 2398.9))   # 63
    t += timedelta(minutes=5)
    out.append(_candle(t, 2398.9, 2400.2, 2398.4, 2399.7))   # 64 confirmation close
    t += timedelta(minutes=5)
    price = 2399.7
    for i in range(55):                                # rise through TP, then chop
        o = price
        c = price + (0.15 if i < 18 else -0.05)
        out.append(_candle(t, o, max(o, c) + 0.25, min(o, c) - 0.2, c))
        price = c
        t += timedelta(minutes=5)
    return out


def _strategy(settings):
    from mercury.core.config import StrategyConfig

    cfg = StrategyConfig(id="xauusd_m5_ict", enabled=True, symbol=SYM, timeframe="M5", ict=_ict_config())
    strategy = ICTStrategy(cfg, settings=settings)
    strategy.set_context_provider(
        lambda symbol, timeframe, count: _h4_candles() if timeframe == "H4" else _h1_candles()
    )
    return strategy


def test_build_h1_snapshot(settings):
    snap = build_h1_snapshot(_h1_candles(), _ict_config())
    assert snap is not None
    assert snap.buy_blocks, "expected at least one unmitigated buy order block"
    levels = [lv["level"] for lv in snap.sell_liquidity]
    assert any(abs(lv - 2398.7) < 0.01 for lv in levels), "expected swing-low liquidity at 2398.7"


def test_ict_strategy_emits_long_signal(settings):
    strategy = _strategy(settings)
    signals = strategy.generate_signals(_m5_candles())
    assert signals, "expected at least one ICT signal"
    longs = [s for s in signals if s.direction.value == "long"]
    assert longs, "expected a long signal"

    signal = longs[0]
    assert signal.sl < signal.price < signal.tp
    assert signal.meta["setup"] == "order_block"
    assert signal.meta["sweep_level"] == 2398.7
    assert signal.meta["structural_level"] == 2398.7
    assert signal.meta["session"] == "london"
    assert signal.meta["bias_h4"] == "long"


def test_ict_no_signals_outside_sessions(settings):
    strategy = _strategy(settings)
    settings.base.trading_sessions = []
    assert strategy.generate_signals(_m5_candles()) == []


def test_detect_opposite_bos():
    candles = []
    t = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
    price = 2400.0
    for _ in range(13):                                   # 0..12 monotonic rise (ATR warmup)
        o = price
        c = price + 0.05
        candles.append(_candle(t, o, c + 0.15, o - 0.15, c))
        price = c
        t += timedelta(minutes=5)
    for row in [                                          # 13..17 swing low at 15 (2399.9)
        (2400.6, 2400.9, 2400.5, 2400.7),
        (2400.7, 2400.8, 2400.3, 2400.4),
        (2400.4, 2400.5, 2399.9, 2400.1),
        (2400.1, 2400.4, 2400.0, 2400.2),
        (2400.2, 2400.6, 2400.1, 2400.4),
    ]:
        candles.append(_candle(t, *row))
        t += timedelta(minutes=5)
    price = 2400.4
    for _ in range(30):                                   # 18..47 rise to ~2402.1
        o = price
        c = price + 0.06
        candles.append(_candle(t, o, c + 0.15, o - 0.15, c))
        price = c
        t += timedelta(minutes=5)
    candles.append(_candle(t, 2402.0, 2402.3, 2398.6, 2398.9))   # 48 big drop, bearish BOS
    assert ind.detect_opposite_bos(candles, "long") == 48
    assert ind.detect_opposite_bos(candles, "short") != 48

    candles = []
    t = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
    price = 2400.0
    for _ in range(13):                                   # 0..12 monotonic decline (ATR warmup)
        o = price
        c = price - 0.05
        candles.append(_candle(t, o, o + 0.15, c - 0.15, c))
        price = c
        t += timedelta(minutes=5)
    for row in [                                          # 13..17 swing high at 15 (2400.2)
        (2399.35, 2399.5, 2399.1, 2399.3),
        (2399.3, 2399.6, 2399.2, 2399.5),
        (2399.5, 2400.2, 2399.5, 2400.0),
        (2400.0, 2400.1, 2399.6, 2399.7),
        (2399.7, 2399.9, 2399.4, 2399.5),
    ]:
        candles.append(_candle(t, *row))
        t += timedelta(minutes=5)
    price = 2399.5
    for _ in range(30):                                   # 18..47 decline to ~2397.7
        o = price
        c = price - 0.06
        candles.append(_candle(t, o, o + 0.15, c - 0.15, c))
        price = c
        t += timedelta(minutes=5)
    candles.append(_candle(t, 2396.7, 2401.0, 2396.6, 2400.6))   # 48 big rally, bullish BOS
    assert ind.detect_opposite_bos(candles, "short") == 48
    assert ind.detect_opposite_bos(candles, "long") != 48


def test_ict_backtest_executes_trade(settings):
    strategy = _strategy(settings)
    candles = _m5_candles()
    out = run_backtest(strategy, candles, risk_percent=0.5)
    assert out.trades, "expected at least one simulated trade"
    tp_trades = [t for t in out.trades if t.close_reason == "tp"]
    assert tp_trades, "expected a take-profit close"
    assert all(t.pnl_r > 0 for t in tp_trades)
    assert out.metrics["trades"] == len(out.trades)
