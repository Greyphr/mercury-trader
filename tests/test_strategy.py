import math
from datetime import UTC, datetime, timedelta

from mercury.core.config import StrategyConfig
from mercury.core.validation import Candle
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
