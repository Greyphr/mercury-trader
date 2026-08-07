import math
from datetime import UTC, datetime, timedelta

from mercury.core.config import ICTConfig, StrategyConfig
from mercury.core.validation import Candle
from mercury.services.strategy import ict as ict_mod
from mercury.services.strategy.ict import ICTStrategy
from mercury.services.strategy.strategies import TrendFollowingStrategy


def make_candles(n=400, trend=0.03, volatility=0.4, start=100.0):
    """Oscillating-uptrend candles so EMA fast/slow actually cross."""
    candles = []
    t = datetime.now(UTC) - timedelta(minutes=5 * n)
    price = start
    for i in range(n):
        wave = 0.8 * math.sin(i / 7)
        price += trend + wave + (0.3 if i % 5 == 0 else -0.2)
        o = price
        c = price + 0.05
        hi = max(o, c) + volatility
        lo = min(o, c) - volatility
        candles.append(
            Candle(
                symbol="XAUUSD", timeframe="M5",
                time=t + timedelta(minutes=5 * i),
                open=o, high=hi, low=lo, close=c, volume=100,
            )
        )
        price = c
    return candles


def test_trend_strategy_emits_signals():
    cfg = StrategyConfig(
        id="test_trend", enabled=True, symbol="XAUUSD", timeframe="M5",
        entry={"fast_ema_period": 9, "slow_ema_period": 21, "trend_ema_period": 50,
               "rsi_period": 14, "atr_period": 14},
    )
    strategy = TrendFollowingStrategy(cfg)
    signals = strategy.generate_signals(make_candles(n=400, trend=0.03))
    assert len(signals) > 0
    assert all(s.sl and s.tp for s in signals)


def test_trend_strategy_sets_atr_levels():
    cfg = StrategyConfig(
        id="test_trend", enabled=True, symbol="XAUUSD", timeframe="M5",
    )
    strategy = TrendFollowingStrategy(cfg)
    signals = strategy.generate_signals(make_candles(n=400, trend=0.05))
    assert len(signals) > 0
    for s in signals:
        if s.direction.value == "long":
            assert s.tp > s.price > s.sl
        else:
            assert s.sl > s.price > s.tp


def test_trend_strategy_respects_direction_config():
    cfg = StrategyConfig(
        id="test_trend", enabled=True, symbol="XAUUSD", timeframe="M5",
        order={"direction": "long"},
    )
    strategy = TrendFollowingStrategy(cfg)
    signals = strategy.generate_signals(make_candles(n=400, trend=-0.03))
    assert all(s.direction.value == "long" for s in signals)


def _series(symbol, timeframe, n, step, end, price=2400.0):
    """Ascending-time candle series ending at ``end`` (fixed, deterministic)."""
    candles = []
    p = price
    for i in range(n - 1, -1, -1):
        wave = 0.5 * math.sin((n - 1 - i) / 5)
        p += 0.08 + wave
        o = p
        c = p + 0.1
        candles.append(
            Candle(
                symbol=symbol, timeframe=timeframe, time=end - i * step,
                open=o, high=max(o, c) + 0.8, low=min(o, c) - 0.8,
                close=c, volume=100,
            )
        )
    return candles


def test_ict_h1_snapshot_cached_across_ticks(monkeypatch):
    """The H1 snapshot (incl. detect_trendlines) must not be recomputed on
    M5 ticks with unchanged H1 data — only when a new H1 candle is added."""
    t_end = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    m5 = _series("XAUUSD", "M5", 200, timedelta(minutes=5), t_end)
    h1 = _series("XAUUSD", "H1", 60, timedelta(hours=1), t_end - timedelta(hours=2))
    h4 = _series("XAUUSD", "H4", 24, timedelta(hours=4), t_end - timedelta(hours=4))

    state = {"h1": h1, "h4": h4}

    def provider(symbol, timeframe, count):
        return state["h1"] if timeframe == "H1" else state["h4"]

    cfg = StrategyConfig(
        id="ict_test", enabled=True, symbol="XAUUSD", timeframe="M5",
        ict=ICTConfig(),
    )
    strategy = ICTStrategy(cfg, context_provider=provider)

    calls = {"n": 0}
    real = ict_mod.build_h1_snapshot

    def counting(prefix, ict):
        calls["n"] += 1
        return real(prefix, ict)

    monkeypatch.setattr(ict_mod, "build_h1_snapshot", counting)

    strategy.generate_signals(m5)
    first = calls["n"]
    assert first > 0

    strategy.generate_signals(m5)
    assert calls["n"] == first

    new_h1_candle = _series("XAUUSD", "H1", 1, timedelta(hours=1),
                            t_end - timedelta(hours=1))[0]
    state["h1"] = h1 + [new_h1_candle]
    strategy.generate_signals(m5)
    assert calls["n"] == first + 1

    new_m5_candle = _series("XAUUSD", "M5", 1, timedelta(minutes=5),
                            t_end + timedelta(minutes=5))[0]
    strategy.generate_signals(m5 + [new_m5_candle])
    assert calls["n"] == first + 1
