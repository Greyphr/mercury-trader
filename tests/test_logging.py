"""Tests for console-safe logging (emoji on cp1252 Windows consoles)."""

from __future__ import annotations

import io

from mercury.core.logging import _console_stream, _ReplacingWriter


class _RaisingStream:
    """A stream whose write() raises UnicodeEncodeError on non-cp1252 text."""

    encoding = "cp1252"

    def __init__(self) -> None:
        self.data = b""

    def write(self, message: str) -> None:
        self.data += message.encode("cp1252")  # raises on emoji

    def flush(self) -> None:
        pass


def test_console_stream_never_raises_on_emoji_under_cp1252():
    bio = io.BytesIO()
    stream = io.TextIOWrapper(bio, encoding="cp1252")
    safe = _console_stream(stream)
    safe.write("✅ startup validation passed — trading enabled\n")
    safe.flush()
    assert b"startup validation passed" in bio.getvalue()


def test_replacing_writer_survives_hard_cp1252_stream():
    stream = _RaisingStream()
    writer = _ReplacingWriter(stream)
    writer.write("✅ trade closed +1.23 USDT\n")  # must not raise
    writer.flush()
    assert b"trade closed" in stream.data


def test_replacing_writer_keeps_ascii_intact():
    bio = io.BytesIO()
    stream = io.TextIOWrapper(bio, encoding="ascii", errors="strict", newline="")
    writer = _ReplacingWriter(stream)
    writer.write("plain ascii log line\n")
    writer.flush()
    assert bio.getvalue() == b"plain ascii log line\n"


def test_full_logger_can_emit_emoji_without_crashing():
    import logging

    import mercury.core.logging as core_logging

    logger = logging.getLogger(f"mercury.test_emoji_{id(bio := io.BytesIO())}")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    stream = io.TextIOWrapper(bio, encoding="cp1252")
    handler = logging.StreamHandler(core_logging._console_stream(stream))
    handler.setFormatter(logging.Formatter(core_logging.LOG_FORMAT, core_logging.DATE_FORMAT))
    logger.addHandler(handler)

    logger.info("✅ NOTIFICATION [info] Startup Validation: ❌ broker disconnected")
    handler.flush()
    assert b"NOTIFICATION" in bio.getvalue()
