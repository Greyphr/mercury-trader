"""Structured logging setup and helpers.

Uses the standard library with a compact structured formatter so logs are
both human-readable on console and parseable in files (optional JSON).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class _ReplacingWriter:
    """Write wrapper that never raises on un-encodable characters.

    cp1252 Windows consoles cannot encode emoji (✅, ❌, …); without this,
    ``StreamHandler`` catches the ``UnicodeEncodeError`` and spams a
    ``--- Logging error ---`` traceback on every such log line. Un-encodable
    characters are replaced instead of crashing the log call.
    """

    def __init__(self, stream: Any) -> None:
        self._stream = stream

    def write(self, message: str) -> int:
        try:
            self._stream.write(message)
        except UnicodeEncodeError:
            encoding = getattr(self._stream, "encoding", None) or "utf-8"
            self._stream.write(message.encode(encoding, "replace").decode(encoding, "replace"))
        return len(message)

    def flush(self) -> None:
        self._stream.flush()

    def isatty(self) -> bool:
        try:
            return bool(self._stream.isatty())
        except Exception:  # noqa: BLE001
            return False


def _console_stream(stream: Any) -> Any:
    """Return a safe write target for the console log handler.

    Best-effort: relax the stream's error handling to ``replace`` (no encoding
    change, so consumers keep their expected byte layout) and always wrap it
    with :class:`_ReplacingWriter` as a guaranteed no-crash fallback.
    """
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass
    return _ReplacingWriter(stream)


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per line (useful for file sinks)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, DATE_FORMAT),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload["extra"] = extra
        return json.dumps(payload, default=str)


def _configure(level: str, log_dir: str | Path | None) -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers.clear()

    console = logging.StreamHandler(_console_stream(sys.stdout))
    console.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    root.addHandler(console)

    if log_dir:
        path = Path(log_dir)
        path.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path / "mercury.log", encoding="utf-8")
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)


def setup_logging(level: str = "INFO", log_dir: str | Path | None = "logs") -> None:
    """Configure root logger. Safe to call multiple times (idempotent)."""
    _configure(level, log_dir)


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the ``mercury`` namespace."""
    return logging.getLogger(f"mercury.{name}")


def log_extra(**fields: Any) -> dict[str, Any]:
    """Helper: mark fields for inclusion in JSON file output.

    Usage: ``logger.info("trade", extra={"extra_fields": log_extra(id=1)})``
    """
    return fields
