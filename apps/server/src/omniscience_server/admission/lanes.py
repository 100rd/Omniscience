"""SRE incident vs background admission lanes (issue #350, AC-SCALE-2).

Two named, separately-bounded lanes: `INCIDENT_LANE` for SRE incident reads
and `BACKGROUND_LANE` for everything else (default agent/search traffic).
Lane identity is **server-derived** from the authenticated token's granted
scopes — never from a client-supplied header or request field — so a
consumer cannot self-declare into the incident lane and bypass the
background budget (task-spec "Out of scope: allowing consumer identity to
bypass ... rules").

A token carrying `incidents:write` or `admin` is treated as SRE-incident
capable, reusing the existing `Scope` enum rather than inventing a parallel
identity concept — the same scope SPEC-MCP's `omniscience-mcp-read-v1`
profile precedent already gates incident-response tools on.
"""

from __future__ import annotations

from collections.abc import Iterable

from omniscience_core.auth.scopes import Scope
from omniscience_core.db.models import ApiToken

INCIDENT_LANE = "incident"
BACKGROUND_LANE = "background"

_INCIDENT_CAPABLE_SCOPES: frozenset[str] = frozenset(
    {Scope.incidents_write.value, Scope.admin.value}
)


def resolve_admission_lane(token: ApiToken) -> str:
    """Return `INCIDENT_LANE` or `BACKGROUND_LANE` for `token`.

    Derived solely from `token.scopes` (resolved server-side from the
    authenticated bearer token during auth) — no client-controlled input
    participates in this decision.
    """
    scopes: Iterable[str] = getattr(token, "scopes", None) or ()
    if _INCIDENT_CAPABLE_SCOPES.intersection(scopes):
        return INCIDENT_LANE
    return BACKGROUND_LANE


__all__ = [
    "BACKGROUND_LANE",
    "INCIDENT_LANE",
    "resolve_admission_lane",
]
