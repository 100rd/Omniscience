"""Runbook source connector for Omniscience (issue #231).

Parses CommonMark runbooks from filesystem trees, extracts YAML front-matter,
headings, and fenced code blocks, and emits one bitemporal entity per runbook
plus structured per-step bodies that downstream tooling
(:mod:`omniscience_server.runbook`) consumes to suggest matching runbooks at
incident time.

The connector reuses the existing filesystem walking semantics but is *not*
a subclass of :class:`FsConnector` — runbooks have a richer DocumentRef
metadata block (parsed front-matter, step ids, alert-name globs) and a
distinct ``runbook://`` canonical-name space so the entity linker can attach
``MATCHES`` edges from alerts.

Canonical names
---------------
* Runbook entity         — ``runbook://{source_uid}/{relative_path}``
* Runbook step entity    — ``runbook://{source_uid}/{relative_path}#{step_id}``

The ``relative_path`` is POSIX-normalised and stripped of leading slashes so
runbooks discovered through git, fs, and Confluence ingest paths produce
byte-identical names (cross_ref enabled).

Front-matter schema (recognised keys; unknown keys are passed through as
metadata but ignored by the matcher):

    ---
    title: "DB connections exhausted"
    alert_names:                # exact-match candidates
      - "alert://datadog/db.connections.exhausted"
    alert_patterns:             # fnmatch-style globs
      - "alert://datadog/db.*"
    tags:                       # free-form, joined to alert tags
      - "service:payments"
      - "severity:sev2"
    severity: sev2              # optional canonical severity slot
    owners:
      - "team:payments"
    ---

Public surface
--------------

>>> from omniscience_connectors.runbook import (
...     RunbookConfig,
...     RunbookConnector,
...     RunbookDocument,
...     RunbookStep,
...     RunbookFrontMatter,
...     parse_runbook,
...     canonical_runbook_name,
...     canonical_runbook_step_name,
... )
"""

from omniscience_connectors.runbook.connector import (
    RunbookConfig,
    RunbookConnector,
)
from omniscience_connectors.runbook.models import (
    RUNBOOK_NAME_PATTERN,
    RUNBOOK_STEP_NAME_PATTERN,
    RunbookDocument,
    RunbookFrontMatter,
    RunbookStep,
    canonical_runbook_name,
    canonical_runbook_step_name,
)
from omniscience_connectors.runbook.parser import (
    ParseError,
    parse_runbook,
)

__all__ = [
    "RUNBOOK_NAME_PATTERN",
    "RUNBOOK_STEP_NAME_PATTERN",
    "ParseError",
    "RunbookConfig",
    "RunbookConnector",
    "RunbookDocument",
    "RunbookFrontMatter",
    "RunbookStep",
    "canonical_runbook_name",
    "canonical_runbook_step_name",
    "parse_runbook",
]
