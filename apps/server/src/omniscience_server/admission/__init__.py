"""Shared read-admission package (issue #350, AC-SCALE-1..5).

Replaces the process-local rate-limit bypass with a narrow
`AdmissionBackend` interface, a fail-closed state machine for shared-backend
loss, server-derived SRE incident vs background lanes, and a horizontal-read
qualification harness. See `docs/runbooks/read-admission.md`.
"""

from __future__ import annotations

from omniscience_server.admission.backend import (
    AdmissionBackend,
    AdmissionBackendUnavailableError,
    AdmissionDecision,
    BackendHealth,
)
from omniscience_server.admission.lanes import (
    BACKGROUND_LANE,
    INCIDENT_LANE,
    resolve_admission_lane,
)
from omniscience_server.admission.process_local import ProcessLocalAdmissionBackend
from omniscience_server.admission.shared import SharedAdmissionBackend
from omniscience_server.admission.state_machine import (
    AdmissionOutcome,
    FailClosedAdmissionController,
    LeaseAuthority,
    NullLeaseAuthority,
)

__all__ = [
    "BACKGROUND_LANE",
    "INCIDENT_LANE",
    "AdmissionBackend",
    "AdmissionBackendUnavailableError",
    "AdmissionDecision",
    "AdmissionOutcome",
    "BackendHealth",
    "FailClosedAdmissionController",
    "LeaseAuthority",
    "NullLeaseAuthority",
    "ProcessLocalAdmissionBackend",
    "SharedAdmissionBackend",
    "resolve_admission_lane",
]
