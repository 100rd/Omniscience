"""Unit tests for the `AdmissionBackend` Protocol implementations
(issue #350, AC-SCALE-1): `ProcessLocalAdmissionBackend` (behaviour parity
with the pre-#350 token bucket) and `SharedAdmissionBackend` (the
fail-closed stub — never a silent process-local fallback).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from omniscience_server.admission.backend import AdmissionBackendUnavailableError
from omniscience_server.admission.process_local import ProcessLocalAdmissionBackend
from omniscience_server.admission.shared import SharedAdmissionBackend


def test_process_local_allows_burst_within_capacity() -> None:
    backend = ProcessLocalAdmissionBackend()
    for _ in range(5):
        decision = backend.try_admit(lane="l", key="k", capacity=60, refill_per_second=1.0)
        assert decision.allowed is True


def test_process_local_blocks_over_capacity() -> None:
    backend = ProcessLocalAdmissionBackend()
    for _ in range(3):
        backend.try_admit(lane="l", key="k", capacity=3, refill_per_second=3 / 60.0)
    decision = backend.try_admit(lane="l", key="k", capacity=3, refill_per_second=3 / 60.0)
    assert decision.allowed is False
    assert decision.retry_after_seconds > 0


def test_process_local_lanes_are_isolated_per_key() -> None:
    """Different (lane, key) pairs never share or borrow capacity."""
    backend = ProcessLocalAdmissionBackend()
    for _ in range(3):
        backend.try_admit(lane="incident", key="tok", capacity=3, refill_per_second=3 / 60.0)
    # A different lane for the same token key starts with a fresh bucket.
    decision = backend.try_admit(
        lane="background", key="tok", capacity=3, refill_per_second=3 / 60.0
    )
    assert decision.allowed is True


def test_process_local_refills_over_time() -> None:
    backend = ProcessLocalAdmissionBackend()
    for _ in range(60):
        backend.try_admit(lane="l", key="k", capacity=60, refill_per_second=1.0)
    empty = backend.try_admit(lane="l", key="k", capacity=60, refill_per_second=1.0)
    assert empty.allowed is False

    from omniscience_server.admission import process_local as pl_module

    fake_now = pl_module.time.monotonic() + 2.0
    with patch.object(pl_module.time, "monotonic", return_value=fake_now):
        refilled = backend.try_admit(lane="l", key="k", capacity=60, refill_per_second=1.0)
    assert refilled.allowed is True


def test_process_local_reset_and_clear() -> None:
    backend = ProcessLocalAdmissionBackend()
    for _ in range(3):
        backend.try_admit(lane="l", key="k", capacity=3, refill_per_second=3 / 60.0)
    backend.reset(lane="l", key="k")
    assert backend.try_admit(lane="l", key="k", capacity=3, refill_per_second=3 / 60.0).allowed

    for _ in range(3):
        backend.try_admit(lane="l", key="k2", capacity=3, refill_per_second=3 / 60.0)
    backend.clear()
    assert backend.try_admit(lane="l", key="k2", capacity=3, refill_per_second=3 / 60.0).allowed


def test_process_local_health_is_always_healthy() -> None:
    backend = ProcessLocalAdmissionBackend()
    health = backend.health()
    assert health.healthy is True


def test_shared_backend_try_admit_raises_unavailable() -> None:
    """The stub never grants a decision — selecting a shared backend value
    is a fail-closed action, never a silent process-local fallback."""
    backend = SharedAdmissionBackend(backend_identity="shared-redis-lease")
    with pytest.raises(AdmissionBackendUnavailableError, match="shared-redis-lease"):
        backend.try_admit(lane="l", key="k", capacity=10, refill_per_second=1.0)


def test_shared_backend_health_is_unhealthy() -> None:
    backend = SharedAdmissionBackend(backend_identity="shared-redis-lease")
    health = backend.health()
    assert health.healthy is False
