"""Post-mortem generator (issue #232 / H5 Track 1 of epic #230).

Renders a structured post-mortem from the bitemporal graph timeline,
cross-linking runbook-step events when available.

Public surface
--------------

* :func:`generate_postmortem` — top-level composition entry point that
  produces a :class:`PostmortemReport` Pydantic model.
* :func:`render` — renders a :class:`PostmortemReport` to ``markdown`` or
  ``html``.
* :class:`PostmortemTemplate` — Pydantic-typed template definition.
* :func:`get_template` / :func:`list_templates` — template registry.
* :data:`SUPPORTED_TEMPLATES` — closed Literal of registered template ids.

Why a self-contained generator
------------------------------

The post-mortem composes data from three existing pieces:

1. :func:`omniscience_server.incident_timeline.mcp_incident_timeline` —
   the bitemporal "what changed" view, with ``as_of=incident_end``
   anchoring the graph snapshot.
2. :class:`omniscience_server.runbook.RunbookStepRecorder` recent events
   for the incident — the "who did what" view, sourced from the in-
   memory recorder seeded by ``POST /incidents/{id}/runbook-step``.
3. Template registry — three default templates (blameless,
   five-whys, coe) chosen because they are the lingua-franca of incident
   reviews across PagerDuty, incident.io, and the SRE handbook.

The renderer is a pure function over the structured report — no Jinja2
dependency (the codebase deliberately avoids it; templates are
structured Pydantic models rendered via plain f-strings).
"""

from __future__ import annotations

from omniscience_server.postmortem.errors import (
    INCIDENT_NOT_FOUND_CODE,
    INVALID_FORMAT_CODE,
    INVALID_INCIDENT_ID_CODE,
    INVALID_TEMPLATE_CODE,
    PostmortemError,
)
from omniscience_server.postmortem.followups import (
    FollowUp,
    FollowUpPriority,
    extract_followups,
)
from omniscience_server.postmortem.generator import (
    SUPPORTED_FORMATS,
    Citation,
    PostmortemFormat,
    PostmortemReport,
    PostmortemTimelineRow,
    generate_postmortem,
    render,
)
from omniscience_server.postmortem.templates import (
    SUPPORTED_TEMPLATES,
    PostmortemTemplate,
    PostmortemTemplateId,
    get_template,
    list_templates,
)

__all__ = [
    "INCIDENT_NOT_FOUND_CODE",
    "INVALID_FORMAT_CODE",
    "INVALID_INCIDENT_ID_CODE",
    "INVALID_TEMPLATE_CODE",
    "SUPPORTED_FORMATS",
    "SUPPORTED_TEMPLATES",
    "Citation",
    "FollowUp",
    "FollowUpPriority",
    "PostmortemError",
    "PostmortemFormat",
    "PostmortemReport",
    "PostmortemTemplate",
    "PostmortemTemplateId",
    "PostmortemTimelineRow",
    "extract_followups",
    "generate_postmortem",
    "get_template",
    "list_templates",
    "render",
]
