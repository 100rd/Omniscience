"""MCP v1 overload error shape (issue #350, AC-SCALE-3).

`mcp/server.py` (MCP tool dispatch) is out of this task's declared scope —
see `docs/runbooks/read-admission.md` "Scope boundaries" — so no MCP call
site is wired to admission here. This module exists so that follow-up work
has a ready-made, contract-correct error to raise.

Two things matter for AC-SCALE-3's "stable degraded metadata/fallback,
never false-freshness or partial schema-invalid success":

1. `mcp/contracts/v1/schemas/meta.schema.json`'s `fallback.reason` is a
   **closed enum** (`missing_lineage`, `stale_source`, `source_degraded`,
   `never_synced`, `freshness_sla_unknown`, `projection_divergence`,
   `consistency_unknown`, `contract_mismatch`, or `null`) and that schema
   file is not in this task's scope to edit. Inventing a new reason string
   (e.g. an ad hoc `"admission_rejected"`) would *fail* schema validation,
   the opposite of preserving the contract — so this module deliberately
   does **not** build a `meta` object at all for an admission rejection.
2. Every existing MCP call site that must reject a call outright (auth,
   scope, bad request, `as_of` validation — see `mcp/server.py`) raises a
   plain `ValueError` with a stable `"<code>:<message>"` prefix, which the
   MCP framework turns into a stable tool-level error (REQ-MCP-4: never a
   malformed/partial success payload). `raise_admission_error` follows the
   exact same convention, reusing the **same `code` strings as the REST
   429/503 contract** (`rate_limited`, `admission_backend_unavailable`) so
   the two transports stay consistent without either one inventing a
   parallel vocabulary.
"""

from __future__ import annotations

from omniscience_server.admission.state_machine import STATE_BACKEND_LOST, AdmissionOutcome

#: Same `code` value REST's 429 envelope uses (`rest/rate_limit.py`,
#: `rest/errors.py`) — a lane-exhausted admission rejection.
RATE_LIMITED_CODE = "rate_limited"

#: Same `code` value REST's 503 envelope uses (`rest/rate_limit.py`) — the
#: shared admission backend is unavailable (AC-SCALE-4 backend-loss path).
ADMISSION_BACKEND_UNAVAILABLE_CODE = "admission_backend_unavailable"


def admission_error_code(outcome: AdmissionOutcome) -> str:
    """Return the stable error code for a rejected `AdmissionOutcome` —
    the same code REST would use for the equivalent 429/503."""
    if outcome.state == STATE_BACKEND_LOST:
        return ADMISSION_BACKEND_UNAVAILABLE_CODE
    return RATE_LIMITED_CODE


def raise_admission_error(outcome: AdmissionOutcome) -> None:
    """Raise the stable `ValueError("<code>:<message>")` a future MCP call
    site should call on a rejected `AdmissionOutcome` — mirrors every
    existing rejection in `mcp/server.py` (e.g. `"forbidden:..."`,
    `"bad_request:..."`), which the MCP framework turns into a stable
    tool-level error, never a truncated or schema-invalid success.

    Never returns normally — callers use this exactly like the existing
    `raise ValueError(...)` call sites it mirrors.
    """
    code = admission_error_code(outcome)
    message = outcome.reason or code
    raise ValueError(f"{code}:{message}")


__all__ = [
    "ADMISSION_BACKEND_UNAVAILABLE_CODE",
    "RATE_LIMITED_CODE",
    "admission_error_code",
    "raise_admission_error",
]
