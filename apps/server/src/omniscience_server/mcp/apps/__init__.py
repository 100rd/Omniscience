"""MCP Apps UI cards for Omniscience (issue #238).

This subpackage implements the **Jan 2026 MCP Apps spec** (an
experimental MCP capability ``omniscience/apps`` exposed via
``ClientCapabilities.experimental``).  It transforms the raw JSON
returned by Omniscience tools — currently :func:`mcp_resolve_incident`
— into a structured UI card that compatible clients (Claude Code,
Cursor, Cline) render natively.

Design constraints
------------------

* **Backwards compatibility is non-negotiable**.  Clients that do not
  advertise the ``omniscience/apps`` experimental capability receive
  the legacy text/JSON response, byte-for-byte identical to the
  pre-#238 wire format.  See :func:`should_emit_card`.

* **The inner response shape is sacred**.  Wave-3 ticket #233 may add
  an optional ``similar_past`` field to ``ResolveIncidentResponse``;
  the card renderer must (a) never modify the inner dict and (b)
  gracefully no-op if the field is missing.  See
  :mod:`omniscience_server.mcp.apps.card_builders` for the
  field-detection contract.

* **Card schemas are versioned**.  Every emitted card carries an
  ``apps_version`` tag so client renderers can fall back to the legacy
  text representation when they encounter a schema they do not know.
  The current version is :data:`APPS_SCHEMA_VERSION`.

Public surface
--------------

The two entry points relevant to callers are:

* :func:`build_resolve_incident_card` — the pure, side-effect-free
  builder that takes the legacy ``resolve_incident`` dict and returns
  a card payload conforming to
  :class:`~omniscience_server.mcp.apps.card_schemas.ResolveIncidentCard`.

* :func:`wrap_resolve_incident_response` — the wire-format wrapper
  invoked at the MCP tool boundary.  It performs client-capability
  detection and decides whether to attach the card.
"""

from __future__ import annotations

from omniscience_server.mcp.apps.capability import (
    APPS_CAPABILITY_KEY,
    APPS_SCHEMA_VERSION,
    client_supports_apps,
    should_emit_card,
)
from omniscience_server.mcp.apps.card_builders import (
    build_resolve_incident_card,
    wrap_resolve_incident_response,
)
from omniscience_server.mcp.apps.card_schemas import (
    CitationLink,
    ConfidenceBar,
    IncidentSummary,
    RankedCandidate,
    ResolveIncidentCard,
    SimilarPastIncident,
)

__all__ = [
    "APPS_CAPABILITY_KEY",
    "APPS_SCHEMA_VERSION",
    "CitationLink",
    "ConfidenceBar",
    "IncidentSummary",
    "RankedCandidate",
    "ResolveIncidentCard",
    "SimilarPastIncident",
    "build_resolve_incident_card",
    "client_supports_apps",
    "should_emit_card",
    "wrap_resolve_incident_response",
]
