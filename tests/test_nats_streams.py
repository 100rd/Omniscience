"""Regression tests for JetStream stream setup (issue: streams never created).

Two bugs are covered:
1. ``max_age`` was passed in nanoseconds; nats-py expects SECONDS and converts
   to ns in ``as_dict()`` — the old value double-converted to a 24-digit number
   the server rejected with err_code 10025 "invalid JSON".
2. ``_ensure_stream`` swallowed *every* BadRequestError as "already exists",
   masking the invalid-config failure so no stream was ever created.
"""

from unittest.mock import AsyncMock

import pytest
from nats.js.errors import BadRequestError
from omniscience_core.queue.streams import (
    _STREAM_ALREADY_EXISTS_CODE,
    _STREAM_CONFIGS,
    _ensure_stream,
)

_SEVEN_DAYS_NS = 7 * 24 * 60 * 60 * 10**9


def test_stream_config_max_age_serialises_to_seven_days_ns() -> None:
    """as_dict() must yield exactly 7 days in nanoseconds (not a 24-digit value)."""
    assert _STREAM_CONFIGS, "expected at least one stream config"
    for cfg in _STREAM_CONFIGS:
        assert cfg.as_dict()["max_age"] == _SEVEN_DAYS_NS


def _bad_request(err_code: int) -> BadRequestError:
    exc = BadRequestError.__new__(BadRequestError)
    exc.err_code = err_code
    exc.code = 400
    exc.description = "test"
    return exc


@pytest.mark.asyncio
async def test_ensure_stream_swallows_only_already_exists() -> None:
    """err_code 10058 (already in use) is benign; nothing is raised."""
    js = AsyncMock()
    js.add_stream = AsyncMock(side_effect=_bad_request(_STREAM_ALREADY_EXISTS_CODE))
    await _ensure_stream(js, _STREAM_CONFIGS[0])  # must not raise


@pytest.mark.asyncio
async def test_ensure_stream_reraises_invalid_config() -> None:
    """A non-exists BadRequestError (e.g. 10025 invalid JSON) must propagate."""
    js = AsyncMock()
    js.add_stream = AsyncMock(side_effect=_bad_request(10025))
    with pytest.raises(BadRequestError):
        await _ensure_stream(js, _STREAM_CONFIGS[0])
