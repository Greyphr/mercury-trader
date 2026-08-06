from datetime import UTC

import numpy as np

from mercury.services.strategy.indicators import atr, ema, rsi, sma


def test_sma_known_values():
    values = np.array([1.0, 2.0, 3.0, 4.0], dtype=float)
    out = sma(values, 2)
    assert out[0] == 0 or np.isnan(out[0])
    assert out[1] == 1.5
    assert out[2] == 2.5


def test_ema_length_and_finite():
    values = np.arange(1.0, 60.0)
    out = ema(values, 10)
    assert len(out) == len(values)
    assert np.all(np.isfinite(out[10:]))
    assert np.isnan(out[0])


def test_rsi_bounds():
    values = np.sin(np.linspace(0, 10, 200)) * 5 + 100
    out = rsi(values, 14)
    assert np.all(out[15:] >= 0)
    assert np.all(out[15:] <= 100)


def test_atr_positive():
    from datetime import datetime, timedelta

    from mercury.core.validation import Candle

    candles = [
        Candle(
            symbol="XAUUSD", timeframe="M5",
            time=datetime.now(UTC) - timedelta(minutes=5 * i),
            open=100 + i, high=103 + i, low=98 + i, close=101 + i, volume=1,
        )
        for i in range(20)
    ]
    out = atr(candles, 14)
    assert out[13] > 0
    assert np.all(out[13:] > 0)
