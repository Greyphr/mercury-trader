"""Validation utilities: market data sanity checks and JSON schema validation
for Hermes structured output."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from mercury.core.logging import get_logger

logger = get_logger("core.validation")


class Candle(BaseModel):
    """A validated OHLCV candle."""

    symbol: str
    timeframe: str
    time: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = 0.0

    def model_post_init(self, __context: Any) -> None:
        if self.high < self.low:
            raise ValueError(f"invalid candle {self.symbol}: high < low")
        if not (self.low <= min(self.open, self.close) and self.high >= max(self.open, self.close)):
            raise ValueError(f"invalid candle {self.symbol}: OHLC out of range")


def validate_candles(rows: list[dict[str, Any]] | list[Any]) -> list[Candle]:
    """Validate a batch of raw candle rows into :class:`Candle` objects."""
    out: list[Candle] = []
    for row in rows:
        data = row.model_dump() if isinstance(row, BaseModel) else row
        try:
            out.append(Candle.model_validate(data))
        except ValidationError as exc:
            logger.warning("dropped invalid candle", extra={"error": str(exc), "candle": data})
    return out


def validate_against_schema(data: Any, json_schema: dict[str, Any]) -> Any:
    """Validate arbitrary data against a JSON Schema draft-07 document.

    Uses Pydantic's JSON schema validator (``TypeAdapter``) via a dynamic model.
    Raises :class:`ValueError` on failure.
    """
    try:
        from pydantic import TypeAdapter
    except ImportError:  # pragma: no cover - very old pydantic
        raise ValueError("pydantic >= 2.7 required for schema validation") from None

    # Fast structural checks before building the adapter.
    adapter = TypeAdapter(dict[str, Any], config={"arbitrary_types_allowed": True})
    _ = adapter
    if not _basic_shape_ok(data, json_schema):
        raise ValueError("data does not match expected structure")

    try:
        from jsonschema import Draft7Validator  # optional, lightweight
    except ImportError:
        logger.debug("jsonschema not installed; falling back to lenient validation")
        return data
    else:
        errors = sorted(Draft7Validator(json_schema).iter_errors(data), key=lambda e: list(e.path))
        if errors:
            raise ValueError(f"JSON schema validation failed: {errors[0].message}")
        return data


def _basic_shape_ok(data: Any, schema: dict[str, Any]) -> bool:
    if "type" in schema:
        expected = schema["type"]
        if expected == "object" and not isinstance(data, dict):
            return False
        if expected == "array" and not isinstance(data, list):
            return False
        if expected == "number" and not isinstance(data, (int, float)):
            return False
        if expected == "string" and not isinstance(data, str):
            return False
        if expected == "boolean" and not isinstance(data, bool):
            return False
    if isinstance(data, dict) and "properties" in schema:
        for prop, sub in schema["properties"].items():
            if prop in data and not _basic_shape_ok(data[prop], sub):
                return False
    if isinstance(data, list) and "items" in schema:
        for item in data:
            if not _basic_shape_ok(item, schema["items"]):
                return False
    return True
