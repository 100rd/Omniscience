"""Fail-closed admission state machine (issue #350, AC-SCALE-4).

`FailClosedAdmissionController` wraps an `AdmissionBackend` and answers
every admission request through it. When the backend is healthy, behaviour
is exactly `backend.try_admit(...)`. When the backend is unreachable
(`health().healthy is False`) or raises `AdmissionBackendUnavailableError`:

- `BACKGROUND_LANE` is always rejected — background admission stops
  entirely, with no exception.
- `INCIDENT_LANE` is rejected UNLESS a *separate*, independently-queryable
  `LeaseAuthority` grants a bounded incident-reserve lease. This is
  deliberately not the same backend that just failed — asking the backend
  that is down to also authorize the reserve would prove nothing. No real
  `LeaseAuthority` is wired in this repository (issue #350 decision-return:
  provisioning a durable lock/lease service is a human decision); the safe
  default, `NullLeaseAuthority`, always answers "not authorized", so an
  unconfigured deployment fails closed rather than silently granting a
  reserve nobody actually proved.

At no point does this controller fall back to a fresh
`ProcessLocalAdmissionBackend` or any other per-process bucket when the
configured backend is unhealthy — that bypass is exactly what AC-SCALE-4
forbids ("never falls back to per-process buckets").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import structlog

from omniscience_server.admission.backend import (
    AdmissionBackend,
    AdmissionBackendUnavailableError,
    BackendHealth,
)
from omniscience_server.admission.lanes import BACKGROUND_LANE

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# States / outcomes
# ---------------------------------------------------------------------------

STATE_NORMAL = "normal"
STATE_BACKEND_LOST = "backend_lost"

REASON_LANE_EXHAUSTED = "lane_exhausted"
REASON_BACKGROUND_SUSPENDED = "backend_loss_background_suspended"
REASON_NO_PROVABLE_AUTHORITY = "backend_loss_no_provable_authority"
REASON_INCIDENT_RESERVE = "incident_reserve"


@dataclass(frozen=True)
class AdmissionOutcome:
    """Result of one `FailClosedAdmissionController.evaluate` call."""

    allowed: bool
    state: str
    reason: str | None
    retry_after_seconds: float = 0.0


# ---------------------------------------------------------------------------
# Incident-reserve authority
# ---------------------------------------------------------------------------


class LeaseAuthority(Protocol):
    """Source of provable authority for the bounded incident reserve during
    a shared-backend outage (AC-SCALE-4). Must be answerable independently
    of the primary `AdmissionBackend` — if it shared the same failure
    domain it could not prove anything while that backend is down."""

    def try_acquire_incident_reserve(self, *, key: str, capacity: int) -> bool:
        """Return True only if a lease/lock genuinely grants `key` a slot
        in the bounded incident reserve right now."""
        ...


class NullLeaseAuthority:
    """Fail-closed default `LeaseAuthority`: never grants a reserve slot.

    Used whenever no real durable lease/lock service has been wired — an
    unconfigured deployment must never *assume* it holds authority it has
    not proven (issue #350 decision-return; see
    `docs/runbooks/read-admission.md`)."""

    def try_acquire_incident_reserve(self, *, key: str, capacity: int) -> bool:
        return False


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class FailClosedAdmissionController:
    """Routes `(lane, key)` admission requests through `backend`, degrading
    to the AC-SCALE-4 fail-closed posture when `backend` is unhealthy."""

    def __init__(
        self,
        *,
        backend: AdmissionBackend,
        incident_reserve_capacity: int = 0,
        lease_authority: LeaseAuthority | None = None,
    ) -> None:
        self._backend = backend
        self._incident_reserve_capacity = incident_reserve_capacity
        self._lease_authority = lease_authority or NullLeaseAuthority()

    def evaluate(
        self,
        *,
        lane: str,
        key: str,
        capacity: int,
        refill_per_second: float,
    ) -> AdmissionOutcome:
        health = self._safe_health()
        if not health.healthy:
            return self._degrade(lane=lane, key=key, reason=health.detail or "backend_unhealthy")

        try:
            decision = self._backend.try_admit(
                lane=lane, key=key, capacity=capacity, refill_per_second=refill_per_second
            )
        except AdmissionBackendUnavailableError as exc:
            return self._degrade(lane=lane, key=key, reason=str(exc))

        return AdmissionOutcome(
            allowed=decision.allowed,
            state=STATE_NORMAL,
            reason=None if decision.allowed else REASON_LANE_EXHAUSTED,
            retry_after_seconds=decision.retry_after_seconds,
        )

    def _safe_health(self) -> BackendHealth:
        try:
            return self._backend.health()
        except Exception as exc:
            return BackendHealth(healthy=False, detail=f"health_check_failed:{exc}")

    def _degrade(self, *, lane: str, key: str, reason: str) -> AdmissionOutcome:
        # `reason` is the raw pre-degrade health detail / exception text —
        # never returned to the client (every branch below substitutes a
        # fixed REASON_* constant into AdmissionOutcome.reason instead, so
        # no backend-internal detail leaks in the 503 body). Captured here,
        # server-side only, so the diagnostic isn't silently dropped for
        # incident debugging. Deliberately omits `key` (the caller's token
        # id) — this is a backend-health log line, not a per-consumer one.
        log.warning("admission_backend_degraded", lane=lane, reason=reason)
        if lane == BACKGROUND_LANE:
            return AdmissionOutcome(
                allowed=False, state=STATE_BACKEND_LOST, reason=REASON_BACKGROUND_SUSPENDED
            )
        granted = self._lease_authority.try_acquire_incident_reserve(
            key=key, capacity=self._incident_reserve_capacity
        )
        if granted:
            return AdmissionOutcome(
                allowed=True, state=STATE_BACKEND_LOST, reason=REASON_INCIDENT_RESERVE
            )
        return AdmissionOutcome(
            allowed=False, state=STATE_BACKEND_LOST, reason=REASON_NO_PROVABLE_AUTHORITY
        )


__all__ = [
    "REASON_BACKGROUND_SUSPENDED",
    "REASON_INCIDENT_RESERVE",
    "REASON_LANE_EXHAUSTED",
    "REASON_NO_PROVABLE_AUTHORITY",
    "STATE_BACKEND_LOST",
    "STATE_NORMAL",
    "AdmissionOutcome",
    "FailClosedAdmissionController",
    "LeaseAuthority",
    "NullLeaseAuthority",
]
