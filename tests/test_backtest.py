import math
from datetime import UTC, datetime, timedelta

from mercury.core.config import StrategyConfig
from mercury.core.validation import Candle
from mercury.services.backtest.engine import run_backtest
from mercury.services.strategy.strategies import TrendFollowingStrategy


def make_candles(n=500, trend=0.03, volatility=0.5, start=100.0):
    candles = []
    t = datetime.now(UTC) - timedelta(minutes=5 * n)
    price = start
    for i in range(n):
        wave = 0.9 * math.sin(i / 7)
        price += trend + wave + (0.3 if i % 5 == 0 else -0.2)
        o = price
        c = price + 0.05
        hi = max(o, c) + volatility
        lo = min(o, c) - volatility
        candles.append(
            Candle(symbol="XAUUSD", timeframe="M5",
                   time=t + timedelta(minutes=5 * i),
                   open=o, high=hi, low=lo, close=c, volume=100)
        )
        price = c
    return candles


def test_backtest_runs_and_reports_metrics():
    cfg = StrategyConfig(id="xauusd_m5_trend", enabled=True, symbol="XAUUSD", timeframe="M5")
    out = run_backtest(TrendFollowingStrategy(cfg), make_candles(), risk_percent=0.5)
    assert isinstance(out.trades, list)
    assert "win_rate" in out.metrics
    assert "expectancy_r" in out.metrics
    assert out.initial_equity > 0


def test_backtest_metrics_shape():
    cfg = StrategyConfig(id="xauusd_m5_trend", enabled=True, symbol="XAUUSD", timeframe="M5")
    out = run_backtest(TrendFollowingStrategy(cfg), make_candles(n=600))
    result = out.to_result()
    assert result["strategy_id"] == "xauusd_m5_trend"
    assert result["trades"] == len(out.trades)
    assert "net_profit" in result


def test_backtest_requires_enough_data():
    cfg = StrategyConfig(id="xauusd_m5_trend", enabled=True, symbol="XAUUSD", timeframe="M5")
    try:
        run_backtest(TrendFollowingStrategy(cfg), make_candles(n=10))
        raise AssertionError("should raise")
    except ValueError:
        pass
