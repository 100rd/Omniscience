"""Shared read-admission dependency for REST (issue #350, AC-SCALE-1..4).

Default posture (``admission_backend="disabled"``, unchanged from pre-#350):
token-bucket admission per token, in-process. Exceeded: HTTP 429 with
Retry-After. This module no longer owns bucket storage directly — it
delegates to ``omniscience_server.admission`` (``ProcessLocalAdmissionBackend``
by default) behind the narrow ``AdmissionBackend`` Protocol, so there is no
process-local bypass left for a future shared backend to replace.

Requests are additionally routed through a named lane — ``incident`` for
SRE-incident-capable tokens, ``background`` for everything else
(AC-SCALE-2) — and through ``FailClosedAdmissionController`` (AC-SCALE-4),
which turns a lost/unhealthy backend into a stable 503
(``admission_backend_unavailable``) for background traffic and for
incident traffic lacking a provable reserve lease, rather than silently
falling back to a fresh process-local bucket.
"""

from __future__ import annotations

import threading
from typing import Any

import structlog
from fastapi import Depends, HTTPException, Request
from omniscience_core.auth.middleware import get_current_token
from omniscience_core.db.models import ApiToken

from omniscience_server.admission.backend import AdmissionBackend
from omniscience_server.admission.lanes import BACKGROUND_LANE, resolve_admission_lane
from omniscience_server.admission.process_local import ProcessLocalAdmissionBackend
from omniscience_server.admission.shared import SharedAdmissionBackend
from omniscience_server.admission.state_machine import (
    STATE_BACKEND_LOST,
    FailClosedAdmissionController,
)

log = structlog.get_logger(__name__)

# Default rate: 60 requests per minute (matches Settings.rate_limit_rpm default)
_DEFAULT_RPM: int = 60

# Lane used by the free (non-lane-aware) check_rate_limit/reset_rate_limit
# helpers below — kept separate from "incident"/"background" so direct
# bucket tests never interact with lane-aware traffic.
_DEFAULT_LANE = "default"

# Module-level Depends singleton to avoid ruff B008
_current_token_dep: Any = Depends(get_current_token)

# Process-local backend singleton — the "disabled" (default) admission
# posture. Reused across requests so bucket state persists; also the
# object the legacy check_rate_limit()/clear_all_buckets() helpers below
# operate on directly, preserving pre-#350 test behaviour byte-for-byte.
_default_backend = ProcessLocalAdmissionBackend()
_default_controller = FailClosedAdmissionController(backend=_default_backend)

# Cache of non-"disabled" backend/controller pairs, keyed by the settings
# fields that determine their identity. Built once per distinct config and
# reused across requests — mirrors _default_controller's module-scope
# caching above, so a future real backend's connection pool / lease state
# is never silently discarded between requests (BP review MEDIUM #5).
_shared_controller_cache: dict[tuple[str, str, float, int], FailClosedAdmissionController] = {}
_shared_controller_cache_lock = threading.Lock()


def check_rate_limit(token_id: str, rpm: int) -> tuple[bool, float]:
    """Check and update the token bucket for the given token_id.

    Uses a token-bucket algorithm where:
    - Bucket capacity = rpm tokens
    - Refill rate = rpm tokens per 60 seconds
    - Each request consumes 1 token

    Delegates to the process-local ``AdmissionBackend`` singleton on the
    ``"default"`` lane — kept as a free function for direct unit testing
    and for parity with pre-#350 callers.

    Args:
        token_id: Unique identifier for the API token (string form of UUID).
        rpm: Requests per minute limit.

    Returns:
        (allowed, retry_after_seconds) — if allowed is False, retry_after_seconds
        is the number of seconds until one token will be available.
    """
    decision = _default_backend.try_admit(
        lane=_DEFAULT_LANE, key=token_id, capacity=rpm, refill_per_second=rpm / 60.0
    )
    return decision.allowed, decision.retry_after_seconds


def reset_rate_limit(token_id: str) -> None:
    """Reset the bucket for the given token_id (used in tests)."""
    _default_backend.reset(lane=_DEFAULT_LANE, key=token_id)


def clear_all_buckets() -> None:
    """Clear all rate-limit buckets (used in tests)."""
    _default_backend.clear()


def _lane_capacity_and_bucket(settings: Any, lane: str) -> tuple[int, str]:
    """Resolve the (capacity, bucket_key) pair for `lane`.

    When a lane-specific budget is explicitly configured (HA/enabled
    posture), the lane gets its own bounded bucket — separate from every
    other lane, never shared or borrowed capacity (AC-SCALE-2). When
    unconfigured (the default "disabled" posture, no lane budgets set),
    every lane resolves to the single shared ``_DEFAULT_LANE`` bucket at
    ``rate_limit_rpm`` capacity — byte-for-byte the pre-#350 single-bucket
    behaviour, including for direct ``check_rate_limit`` test callers that
    share the same bucket namespace.
    """
    if settings is not None:
        budget_field = (
            "admission_lane_incident_budget"
            if lane != BACKGROUND_LANE
            else "admission_lane_background_budget"
        )
        budget = getattr(settings, budget_field, None)
        if budget is not None:
            return int(budget), lane
    rpm = (
        int(getattr(settings, "rate_limit_rpm", _DEFAULT_RPM))
        if settings is not None
        else (_DEFAULT_RPM)
    )
    return rpm, _DEFAULT_LANE


def _controller_for(settings: Any) -> FailClosedAdmissionController:
    """Resolve the AdmissionBackend + fail-closed controller for `settings`.

    "disabled" (default) always reuses the module-level process-local
    singleton so bucket state persists across requests. Any other value
    builds a `SharedAdmissionBackend` — a fail-closed stub today (issue
    #350: activating a new infrastructure dependency is a human decision) —
    wrapped in the same `FailClosedAdmissionController` state machine, so an
    "enabled but unimplemented" backend degrades exactly like a real one
    that has gone unreachable, never a silent process-local fallback.

    Non-"disabled" backend/controller pairs are cached by the config fields
    that determine their identity (`_shared_controller_cache`) rather than
    rebuilt on every call — a fresh `SharedAdmissionBackend` per request is
    harmless for today's stateless stub, but would silently discard any
    connection pool or lease-tracking state a real implementation holds.
    See `reset_admission_controller_cache()` for the test-only escape
    hatch.
    """
    mode = (
        str(getattr(settings, "admission_backend", "disabled"))
        if settings is not None
        else ("disabled")
    )
    if mode == "disabled":
        return _default_controller

    identity = str(getattr(settings, "admission_backend_identity", None) or mode)
    reserve_fraction = float(getattr(settings, "admission_failure_reserve_fraction", 0.0) or 0.0)
    incident_budget = int(getattr(settings, "admission_lane_incident_budget", 0) or 0)
    cache_key = (mode, identity, reserve_fraction, incident_budget)

    with _shared_controller_cache_lock:
        controller = _shared_controller_cache.get(cache_key)
        if controller is None:
            backend: AdmissionBackend = SharedAdmissionBackend(backend_identity=identity)
            controller = FailClosedAdmissionController(
                backend=backend,
                incident_reserve_capacity=int(incident_budget * reserve_fraction),
            )
            _shared_controller_cache[cache_key] = controller
        return controller


def reset_admission_controller_cache() -> None:
    """Clear cached non-"disabled" backend/controller pairs (test helper).

    Mirrors `clear_all_buckets()` for the process-local path — tests that
    construct distinct `Settings` across cases should call this so one
    test's cached controller is never observed by another.
    """
    with _shared_controller_cache_lock:
        _shared_controller_cache.clear()


async def rate_limit_dependency(
    request: Request,
    token: ApiToken = _current_token_dep,
) -> ApiToken:
    """FastAPI dependency: enforce shared, lane-aware read admission.

    Raises:
        HTTPException 429 — lane budget exhausted, includes Retry-After.
        HTTPException 503 — shared admission backend unavailable
            (``admission_backend_unavailable``); background traffic and
            incident traffic without a provable reserve lease both land
            here rather than being silently admitted.

    Returns:
        The authenticated ApiToken (passes through from get_current_token).
    """
    settings = getattr(request.app.state, "settings", None)
    lane = resolve_admission_lane(token)
    capacity, bucket_lane = _lane_capacity_and_bucket(settings, lane)
    controller = _controller_for(settings)
    token_id = str(token.id)

    outcome = controller.evaluate(
        lane=bucket_lane, key=token_id, capacity=capacity, refill_per_second=capacity / 60.0
    )

    if not outcome.allowed:
        if outcome.state == STATE_BACKEND_LOST:
            log.warning(
                "admission_backend_unavailable",
                token_prefix=token.token_prefix,
                lane=lane,
                reason=outcome.reason,
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "admission_backend_unavailable",
                    "message": "Shared admission backend unavailable",
                    "reason": outcome.reason,
                },
            )

        retry_after_int = int(outcome.retry_after_seconds) + 1
        log.warning(
            "rate_limit_exceeded",
            token_prefix=token.token_prefix,
            lane=lane,
            retry_after=retry_after_int,
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
    "reset_admission_controller_cache",
    "reset_rate_limit",
]
