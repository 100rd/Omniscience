"""Unit tests for `FailClosedAdmissionController` (issue #350, AC-SCALE-4):
backend loss must stop background admission, preserve only a bounded
incident reserve when authority is provable (never a heuristic), and never
fall back to a fresh per-process bucket.

All backend failures here are injected fakes — a live shared-backend outage
under real multi-replica load is a decision-return
(docs/runbooks/read-admission.md "Blocked on external — decision returns").
"""

from __future__ import annotations

from dataclasses import dataclass

from omniscience_server.admission.backend import (
    AdmissionBackendUnavailableError,
    AdmissionDecision,
    BackendHealth,
)
from omniscience_server.admission.lanes import BACKGROUND_LANE, INCIDENT_LANE
from omniscience_server.admission.process_local import ProcessLocalAdmissionBackend
from omniscience_server.admission.state_machine import (
    REASON_BACKGROUND_SUSPENDED,
    REASON_INCIDENT_RESERVE,
    REASON_LANE_EXHAUSTED,
    REASON_NO_PROVABLE_AUTHORITY,
    STATE_BACKEND_LOST,
    STATE_NORMAL,
    FailClosedAdmissionController,
    NullLeaseAuthority,
)


@dataclass
class _FakeHealthyBackend:
    """Injectable fake — always healthy, delegates to a real process-local
    backend for deterministic try_admit math."""

    def __post_init__(self) -> None:
        self._inner = ProcessLocalAdmissionBackend()

    def try_admit(self, *, lane: str, key: str, capacity: int, refill_per_second: float):
        return self._inner.try_admit(
            lane=lane, key=key, capacity=capacity, refill_per_second=refill_per_second
        )

    def health(self) -> BackendHealth:
        return BackendHealth(healthy=True)


class _FakeUnhealthyBackend:
    """Injectable fake — reports unhealthy without ever raising."""

    def try_admit(self, *, lane: str, key: str, capacity: int, refill_per_second: float):
        raise AssertionError("try_admit must not be called when health() is unhealthy")

    def health(self) -> BackendHealth:
        return BackendHealth(healthy=False, detail="fake_unreachable")


class _FakeRaisingBackend:
    """Injectable fake — reports healthy but raises on try_admit (e.g. the
    connection dropped between the health check and the call)."""

    def try_admit(self, *, lane: str, key: str, capacity: int, refill_per_second: float):
        raise AdmissionBackendUnavailableError("fake_connection_lost")

    def health(self) -> BackendHealth:
        return BackendHealth(healthy=True)


class _AlwaysGrantsLeaseAuthority:
    def try_acquire_incident_reserve(self, *, key: str, capacity: int) -> bool:
        return True


# ---------------------------------------------------------------------------
# Normal (healthy backend) path
# ---------------------------------------------------------------------------


def test_normal_path_admits_within_capacity() -> None:
    controller = FailClosedAdmissionController(backend=_FakeHealthyBackend())
    outcome = controller.evaluate(
        lane=BACKGROUND_LANE, key="tok", capacity=5, refill_per_second=5 / 60.0
    )
    assert outcome.allowed is True
    assert outcome.state == STATE_NORMAL


def test_normal_path_exhausted_lane_is_rejected_not_degraded() -> None:
    controller = FailClosedAdmissionController(backend=_FakeHealthyBackend())
    for _ in range(3):
        controller.evaluate(
            lane=BACKGROUND_LANE, key="tok", capacity=3, refill_per_second=3 / 60.0
        )
    outcome = controller.evaluate(
        lane=BACKGROUND_LANE, key="tok", capacity=3, refill_per_second=3 / 60.0
    )
    assert outcome.allowed is False
    assert outcome.state == STATE_NORMAL
    assert outcome.reason == REASON_LANE_EXHAUSTED


# ---------------------------------------------------------------------------
# Backend-loss path — unhealthy
# ---------------------------------------------------------------------------


def test_backend_unhealthy_suspends_background_lane() -> None:
    controller = FailClosedAdmissionController(backend=_FakeUnhealthyBackend())
    outcome = controller.evaluate(
        lane=BACKGROUND_LANE, key="tok", capacity=10, refill_per_second=1.0
    )
    assert outcome.allowed is False
    assert outcome.state == STATE_BACKEND_LOST
    assert outcome.reason == REASON_BACKGROUND_SUSPENDED


def test_backend_unhealthy_rejects_incident_without_lease_authority() -> None:
    """No LeaseAuthority is wired by default (NullLeaseAuthority) — the
    safe default is fail-closed, never an assumed reserve grant."""
    controller = FailClosedAdmissionController(backend=_FakeUnhealthyBackend())
    outcome = controller.evaluate(
        lane=INCIDENT_LANE, key="tok", capacity=10, refill_per_second=1.0
    )
    assert outcome.allowed is False
    assert outcome.state == STATE_BACKEND_LOST
    assert outcome.reason == REASON_NO_PROVABLE_AUTHORITY


def test_backend_unhealthy_admits_incident_with_granted_lease() -> None:
    controller = FailClosedAdmissionController(
        backend=_FakeUnhealthyBackend(),
        incident_reserve_capacity=5,
        lease_authority=_AlwaysGrantsLeaseAuthority(),
    )
    outcome = controller.evaluate(
        lane=INCIDENT_LANE, key="tok", capacity=10, refill_per_second=1.0
    )
    assert outcome.allowed is True
    assert outcome.state == STATE_BACKEND_LOST
    assert outcome.reason == REASON_INCIDENT_RESERVE


# ---------------------------------------------------------------------------
# Backend-loss path — try_admit raises mid-call
# ---------------------------------------------------------------------------


def test_backend_raises_unavailable_suspends_background_lane() -> None:
    controller = FailClosedAdmissionController(backend=_FakeRaisingBackend())
    outcome = controller.evaluate(
        lane=BACKGROUND_LANE, key="tok", capacity=10, refill_per_second=1.0
    )
    assert outcome.allowed is False
    assert outcome.state == STATE_BACKEND_LOST
    assert outcome.reason == REASON_BACKGROUND_SUSPENDED


def test_backend_raises_unavailable_rejects_incident_without_lease() -> None:
    controller = FailClosedAdmissionController(backend=_FakeRaisingBackend())
    outcome = controller.evaluate(
        lane=INCIDENT_LANE, key="tok", capacity=10, refill_per_second=1.0
    )
    assert outcome.allowed is False
    assert outcome.reason == REASON_NO_PROVABLE_AUTHORITY


# ---------------------------------------------------------------------------
# health() itself failing must never crash the controller
# ---------------------------------------------------------------------------


class _HealthRaisesBackend:
    def try_admit(self, *, lane: str, key: str, capacity: int, refill_per_second: float):
        raise AssertionError("must not reach try_admit")

    def health(self) -> BackendHealth:
        raise RuntimeError("boom")


def test_health_check_exception_degrades_safely() -> None:
    controller = FailClosedAdmissionController(backend=_HealthRaisesBackend())
    outcome = controller.evaluate(
        lane=BACKGROUND_LANE, key="tok", capacity=10, refill_per_second=1.0
    )
    assert outcome.allowed is False
    assert outcome.state == STATE_BACKEND_LOST


# ---------------------------------------------------------------------------
# Never falls back to a fresh per-process bucket on backend loss
# ---------------------------------------------------------------------------


def test_backend_loss_never_substitutes_a_process_local_bucket() -> None:
    """A capacity that would trivially be admitted by a fresh process-local
    bucket must still be rejected once the shared backend is lost — proving
    the controller never quietly swaps in ProcessLocalAdmissionBackend."""
    controller = FailClosedAdmissionController(backend=_FakeUnhealthyBackend())
    for _ in range(3):
        outcome = controller.evaluate(
            lane=BACKGROUND_LANE, key="fresh-key", capacity=1000, refill_per_second=1000 / 60.0
        )
        assert outcome.allowed is False
        assert outcome.state == STATE_BACKEND_LOST


def test_null_lease_authority_always_denies() -> None:
    authority = NullLeaseAuthority()
    assert authority.try_acquire_incident_reserve(key="k", capacity=5) is False


def test_decision_dataclass_roundtrip() -> None:
    decision = AdmissionDecision(allowed=True, retry_after_seconds=0.0)
    assert decision.allowed is True
    assert decision.retry_after_seconds == 0.0
