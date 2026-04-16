"""Tests for omniscience_core logging configuration."""

from __future__ import annotations

import json
import logging

import pytest
import structlog
from omniscience_core.logging import configure_logging


def test_configure_logging_does_not_raise() -> None:
    """configure_logging completes without raising an exception."""
    configure_logging("WARNING")


def test_json_output(capsys: pytest.CaptureFixture[str]) -> None:
    """Structlog produces parseable JSON output after configuration."""
    configure_logging("DEBUG")
    logger = structlog.get_logger("test")
    logger.info("test_event", key="value")

    captured = capsys.readouterr()
    # Find any JSON line containing our event
    for line in captured.out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("event") == "test_event":
            assert record["key"] == "value"
            return
    pytest.fail("No JSON log line with event='test_event' found in stdout")


def test_log_level_respected(capsys: pytest.CaptureFixture[str]) -> None:
    """Log records below the configured level are suppressed."""
    configure_logging("WARNING")
    logger = structlog.get_logger("test_level")
    logger.debug("should_be_suppressed")
    logger.warning("should_appear")

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "should_be_suppressed" not in combined
    assert "should_appear" in combined


def test_trace_context_absent_without_span(capsys: pytest.CaptureFixture[str]) -> None:
    """When no OTel span is active, trace_id and span_id are absent from logs."""
    configure_logging("DEBUG")
    logger = structlog.get_logger("test_trace")
    logger.info("no_span_event")

    captured = capsys.readouterr()
    for line in captured.out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("event") == "no_span_event":
            # trace_id must not appear when there is no active span
            assert "trace_id" not in record
            return
    pytest.fail("No JSON log line with event='no_span_event' found in stdout")


def test_configure_logging_idempotent() -> None:
    """Calling configure_logging twice does not install duplicate handlers."""
    configure_logging("INFO")
    configure_logging("INFO")
    root = logging.getLogger()
    assert len(root.handlers) == 1
