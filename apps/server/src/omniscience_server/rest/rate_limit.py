"""Token-bucket rate limiter per API token.

Default: 60 requests per minute.
Exceeded: HTTP 429 with Retry-After header (seconds until next token is available).

This is an in-process implementation using a dict of token buckets.
For multi-process deployments a shared store (Redis) would be required;
this is explicitly an MVP/single-process implementation.
"""

from __future__ import annotations

import time
from typing import Any

import structlog
from fastapi import Depends, HTTPException, Request
from omniscience_core.auth.middleware import get_current_token
from omniscience_core.db.models import ApiToken

log = structlog.get_logger(__name__)

# Module-level bucket store: token_id -> (tokens_remaining: float, last_refill: float)
_buckets: dict[str, tuple[float, float]] = {}

# Default rate: 60 requests per minute
_DEFAULT_RPM: int = 60

# Module-level Depends singleton to avoid ruff B008
_current_token_dep: Any = Depends(get_current_token)


def _get_rpm(request: Request) -> int:
    """Return the configured RPM from app settings, falling back to the default."""
    settings = getattr(request.app.state, "settings", None)
    if settings is not None:
        return int(getattr(settings, "rate_limit_rpm", _DEFAULT_RPM))
    return _DEFAULT_RPM


def check_rate_limit(token_id: str, rpm: int) -> tuple[bool, float]:
    """Check and update the token bucket for the given token_id.

    Uses a token-bucket algorithm where:
    - Bucket capacity = rpm tokens
    - Refill rate = rpm tokens per 60 seconds
    - Each request consumes 1 token

    Args:
        token_id: Unique identifier for the API token (string form of UUID).
        rpm: Requests per minute limit.

    Returns:
        (allowed, retry_after_seconds) — if allowed is False, retry_after_seconds
        is the number of seconds until one token will be available.
    """
    now = time.monotonic()
    capacity = float(rpm)
    refill_rate = capacity / 60.0  # tokens per second

    if token_id not in _buckets:
        # Brand-new bucket — start full
        _buckets[token_id] = (capacity - 1.0, now)
        return True, 0.0

    tokens, last_refill = _buckets[token_id]

    # Refill based on elapsed time
    elapsed = now - last_refill
    tokens = min(capacity, tokens + elapsed * refill_rate)

    if tokens >= 1.0:
        _buckets[token_id] = (tokens - 1.0, now)
        return True, 0.0

    # Bucket empty — compute how long until 1 token refills
    retry_after = (1.0 - tokens) / refill_rate
    _buckets[token_id] = (tokens, now)
    return False, retry_after


def reset_rate_limit(token_id: str) -> None:
    """Reset the bucket for the given token_id (used in tests)."""
    _buckets.pop(token_id, None)


def clear_all_buckets() -> None:
    """Clear all rate-limit buckets (used in tests)."""
    _buckets.clear()


async def rate_limit_dependency(
    request: Request,
    token: ApiToken = _current_token_dep,
) -> ApiToken:
    """FastAPI dependency: enforce per-token rate limiting.

    Raises:
        HTTPException 429 — rate limit exceeded, includes Retry-After.

    Returns:
        The authenticated ApiToken (passes through from get_current_token).
    """
    rpm = _get_rpm(request)
    token_id = str(token.id)
    allowed, retry_after = check_rate_limit(token_id, rpm)

    if not allowed:
        retry_after_int = int(retry_after) + 1
        log.warning(
            "rate_limit_exceeded", token_prefix=token.token_prefix, retry_after=retry_after_int
        )
        raise HTTPException(
            status_code=429,
            detail={
                "code": "rate_limited",
                "message": "Rate limit exceeded",
                "retry_after": str(retry_after_int),
            },
            headers={"Retry-After": str(retry_after_int)},
        )

    return token


__all__ = [
    "check_rate_limit",
    "clear_all_buckets",
    "rate_limit_dependency",
    "reset_rate_limit",
]
