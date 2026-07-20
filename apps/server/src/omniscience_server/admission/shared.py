"""Shared/networked `AdmissionBackend` stub (issue #350, AC-SCALE-1).

Deliberately unimplemented. Activating a new infrastructure dependency
(e.g. Redis `SET NX PX` / a Lua CAS script, or a managed rate-limiting
service — see `docs/runbooks/read-admission.md` "Backend recommendation")
is a human decision this agent cannot make on its own. Selecting a
non-`"disabled"` `admission_backend` value is therefore a *fail-closed*
action, never a silent fallback to `ProcessLocalAdmissionBackend` — every
call into this stub raises `AdmissionBackendUnavailableError`, which
`admission.state_machine.FailClosedAdmissionController` turns into the
AC-SCALE-4 backend-loss posture (background suspended, incident admitted
only from a provably-authorized reserve).
"""

from __future__ import annotations

from omniscience_server.admission.backend import (
    AdmissionBackendUnavailableError,
    AdmissionDecision,
    BackendHealth,
)


class SharedAdmissionBackend:
    """Stub target for a future shared/networked `AdmissionBackend`.

    `backend_identity` is the human-approved concrete backend descriptor
    (`Settings.admission_backend_identity`) — carried through so a failure
    message names *what* was requested, never a guess.
    """

    def __init__(self, *, backend_identity: str) -> None:
        self._backend_identity = backend_identity

    def try_admit(
        self,
        *,
        lane: str,
        key: str,
        capacity: int,
        refill_per_second: float,
    ) -> AdmissionDecision:
        raise AdmissionBackendUnavailableError(
            f"shared admission backend {self._backend_identity!r} is not "
            "implemented — no shared backend has been approved and wired "
            "(issue #350 AC-SCALE-1); see docs/runbooks/read-admission.md."
        )

    def health(self) -> BackendHealth:
        return BackendHealth(
            healthy=False,
            detail=f"unimplemented:{self._backend_identity}",
        )


__all__ = ["SharedAdmissionBackend"]
