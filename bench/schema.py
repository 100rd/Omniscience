"""Schema for benchmark incident fixtures (issue #241).

Each fixture in ``bench/incidents/`` is a YAML document with this shape:

.. code-block:: yaml

    id: bench-0001
    category: oom                          # one of CATEGORIES
    description: "Pod OOMKilled after memory-limit reduction"
    alert_payload:                          # opaque dict — passed to MCP server
      provider: pagerduty
      alert_id: INC-2026-04-12-001
      ...
    expected_root_cause:                    # golden label, free-text + canonical
      summary: "PR #4242 reduced memory limit from 1Gi to 256Mi"
      canonical_entities:                   # ranked — index 0 is top-1 target
        - "https://github.com/acme/api/pull/4242"
    expected_runbook_step:                  # golden label, single string
      "Revert PR #4242 and bump memory limit back to 1Gi"
    expected_blast_radius:                  # golden label, set of resources
      - "service/api"
      - "deployment/api"
    notes: optional reviewer-facing prose

The runner does NOT need the YAML to declare entity-level confidence; the
scorer uses ranked ``canonical_entities`` to compute MRR and the MCP
server's own ``confidence_score`` against the binary correctness for the
Brier score.

This module is dependency-light by design: it uses ``dataclasses`` +
manual validation rather than pydantic so the bench corpus can be loaded
without the full server install (a reviewer cloning the repo for the
fixtures should not need a Postgres + Qdrant + Neo4j toolchain).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

#: Topical categories — used for stratified reporting in
#: ``bench/results/<date>.md``.  Add new categories sparingly; the
#: scorer reports per-category breakdowns and a sparse cell looks worse
#: than no cell at all.
CATEGORIES: Final[tuple[str, ...]] = (
    "oom",  # memory limits / OOMKilled
    "cert-expiry",  # TLS cert / secret rotation issues
    "dns",  # DNS resolution / CoreDNS / service discovery
    "deploy-rollback",  # bad deploy, rollback path
    "config-drift",  # config/feature-flag drift
    "dependency-outage",  # upstream/3rd-party outage
    "noisy-neighbor",  # resource contention
    "data-corruption",  # bad migration / dual-write skew
)


class InvalidIncidentError(ValueError):
    """Raised when a fixture fails schema validation.

    The message names the offending field and the fixture id so that
    `python bench/runner.py --validate-corpus` produces actionable
    output even on a large corpus.
    """


@dataclass(frozen=True, slots=True)
class ExpectedRootCause:
    summary: str
    canonical_entities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IncidentFixture:
    """One golden-labelled incident for the benchmark corpus."""

    id: str
    category: str
    description: str
    alert_payload: dict[str, Any]
    expected_root_cause: ExpectedRootCause
    expected_runbook_step: str
    expected_blast_radius: tuple[str, ...]
    notes: str = ""
    source_files: tuple[str, ...] = field(default_factory=tuple)


def parse_incident(raw: dict[str, Any], *, path_hint: str = "<dict>") -> IncidentFixture:
    """Validate ``raw`` (a parsed YAML/JSON dict) and return a fixture.

    Validation is intentionally explicit (rather than handing it to a
    schema library) so the error messages name the exact field and the
    fixture path.
    """

    def _require(key: str, expect_type: type) -> Any:
        if key not in raw:
            raise InvalidIncidentError(f"{path_hint}: missing required field '{key}'")
        value = raw[key]
        if not isinstance(value, expect_type):
            raise InvalidIncidentError(
                f"{path_hint}: field '{key}' must be {expect_type.__name__}, "
                f"got {type(value).__name__}"
            )
        return value

    fixture_id = _require("id", str)
    category = _require("category", str)
    if category not in CATEGORIES:
        raise InvalidIncidentError(f"{path_hint}: category {category!r} not in {CATEGORIES}")
    description = _require("description", str)
    alert_payload = _require("alert_payload", dict)

    rc_raw = _require("expected_root_cause", dict)
    rc_summary = rc_raw.get("summary")
    rc_entities = rc_raw.get("canonical_entities")
    if not isinstance(rc_summary, str) or not rc_summary:
        raise InvalidIncidentError(
            f"{path_hint}: expected_root_cause.summary must be a non-empty string"
        )
    if not isinstance(rc_entities, list) or not rc_entities:
        raise InvalidIncidentError(
            f"{path_hint}: expected_root_cause.canonical_entities must be a non-empty list"
        )
    if not all(isinstance(e, str) and e for e in rc_entities):
        raise InvalidIncidentError(
            f"{path_hint}: expected_root_cause.canonical_entities must be all non-empty strings"
        )

    runbook = _require("expected_runbook_step", str)
    if not runbook:
        raise InvalidIncidentError(f"{path_hint}: expected_runbook_step must be non-empty")

    blast = _require("expected_blast_radius", list)
    if not all(isinstance(b, str) and b for b in blast):
        raise InvalidIncidentError(
            f"{path_hint}: expected_blast_radius must be a list of non-empty strings"
        )

    notes_value = raw.get("notes", "")
    if not isinstance(notes_value, str):
        raise InvalidIncidentError(f"{path_hint}: notes must be a string if present")

    source_files_raw = raw.get("source_files", [])
    if not isinstance(source_files_raw, list) or not all(
        isinstance(s, str) for s in source_files_raw
    ):
        raise InvalidIncidentError(
            f"{path_hint}: source_files must be a list of strings if present"
        )

    return IncidentFixture(
        id=fixture_id,
        category=category,
        description=description,
        alert_payload=alert_payload,
        expected_root_cause=ExpectedRootCause(
            summary=rc_summary,
            canonical_entities=tuple(rc_entities),
        ),
        expected_runbook_step=runbook,
        expected_blast_radius=tuple(blast),
        notes=notes_value,
        source_files=tuple(source_files_raw),
    )
