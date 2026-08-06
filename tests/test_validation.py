from datetime import UTC, datetime

import pytest

from mercury.core.validation import Candle, validate_against_schema, validate_candles


def _candle(open_=100.0, high=102.0, low=99.0, close=101.0, time=None):
    return {
        "symbol": "XAUUSD",
        "timeframe": "M5",
        "time": time or datetime.now(UTC),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": 10,
    }


def test_valid_candle():
    c = Candle.model_validate(_candle())
    assert c.symbol == "XAUUSD"


def test_high_below_low_rejected():
    with pytest.raises(ValueError):
        Candle.model_validate(_candle(high=98.0, low=100.0))


def test_ohlc_out_of_range_rejected():
    with pytest.raises(ValueError):
        Candle.model_validate(_candle(high=95.0))


def test_validate_candles_drops_invalid():
    valid = _candle()
    invalid = _candle(high=1.0, low=200.0)
    out = validate_candles([valid, invalid])
    assert len(out) == 1


def test_schema_validation_ok():
    schema = {
        "type": "object",
        "properties": {"decision": {"type": "string", "enum": ["proceed"]}},
        "required": ["decision"],
    }
    assert validate_against_schema({"decision": "proceed"}, schema)


def test_schema_validation_fails():
    schema = {
        "type": "object",
        "properties": {"confidence": {"type": "number"}},
        "required": ["confidence"],
    }
    with pytest.raises(ValueError):
        validate_against_schema({"confidence": "high"}, schema)
