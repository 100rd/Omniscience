"""Runbook domain logic — suggest + step recording (issue #231).

Pure domain layer for the runbook epic.  Contains:

1. Data types: :class:`RunbookStepEvent`, :class:`RunbookStepRecorder`.
2. Validation helpers: :func:`validate_alert_id`, :func:`validate_runbook_name`,
   :func:`validate_step_id`.
3. Loading helpers: :func:`runbook_from_entity_metadata`.
4. The scoring/suggestion pipeline: :func:`score_runbook_candidates`.

The FastAPI orchestration functions (``suggest_runbook``,
``record_runbook_step``) live in ``apps/server`` and call into these
primitives.  This module has no dependency on FastAPI.
"""

from __future__ import annotations

import re
import uuid
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import structlog
from omniscience_connectors.runbook.linker import (
    AlertView,
    MatchResult,
    score_runbook_against_alert,
)
from omniscience_connectors.runbook.models import (
    RunbookDocument,
    canonical_runbook_step_name,
)
from omniscience_core.storage import EntityNodeView
from prometheus_client import Counter

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------------

ALERT_NOT_FOUND_CODE: Final[str] = "alert_not_found"
INVALID_ALERT_ID_CODE: Final[str] = "invalid_alert_id"
INVALID_ALERT_ID_MESSAGE: Final[str] = (
    "alert_id must be of the form 'alert://{provider}/{provider_alert_id}'"
)
RUNBOOK_NOT_FOUND_CODE: Final[str] = "runbook_not_found"
INVALID_RUNBOOK_NAME_CODE: Final[str] = "invalid_runbook_name"
INVALID_RUNBOOK_NAME_MESSAGE: Final[str] = (
    "runbook_name must be of the form 'runbook://{source_uid}/{rel_path}'"
)
INVALID_STEP_ID_CODE: Final[str] = "invalid_step_id"

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

DEFAULT_TOP_K: Final[int] = 3
MAX_TOP_K: Final[int] = 25
DEFAULT_RECORDER_CAPACITY: Final[int] = 10_000

# ---------------------------------------------------------------------------
# Validation regexes
# ---------------------------------------------------------------------------

_ALERT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^alert://[A-Za-z0-9._-]+/[A-Za-z0-9._:/+-]+$"
)
_RUNBOOK_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^runbook://[A-Za-z0-9._\-/]+/[A-Za-z0-9._\-/]+$"
)
_STEP_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._-]+$")

# ---------------------------------------------------------------------------
# Prometheus surface
# ---------------------------------------------------------------------------

RUNBOOK_SUGGEST_TOTAL: Counter = Counter(
    "omniscience_runbook_suggest_total",
    "Number of suggest_runbook invocations, labelled by outcome.",
    labelnames=["outcome"],
)

RUNBOOK_STEP_EVENTS_TOTAL: Counter = Counter(
    "omniscience_runbook_step_events_total",
    (
        "Number of runbook-step execution events recorded.  Labelled by "
        "workspace prefix only (first 8 hex of the workspace UUID) to "
        "preserve tenant-cardinality bounds."
    ),
    labelnames=["workspace_prefix"],
)

# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunbookStepEvent:
    """A single recorded runbook-step execution."""

    event_id: uuid.UUID
    workspace_id: uuid.UUID
    alert_id: str
    incident_id: str
    runbook_name: str
    step_id: str
    step_name: str
    actor: str | None
    note: str | None
    outcome: str
    valid_from: datetime
    valid_to: datetime | None
    recorded_at: datetime

    def to_payload(self) -> dict[str, Any]:
        """Render as the JSON envelope returned by the REST endpoint."""
        return {
            "event_id": str(self.event_id),
            "incident_id": self.incident_id,
            "alert_id": self.alert_id,
            "runbook_name": self.runbook_name,
            "step_id": self.step_id,
            "step_name": self.step_name,
            "actor": self.actor,
            "note": self.note,
            "outcome": self.outcome,
            "valid_from": self.valid_from.isoformat(),
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "recorded_at": self.recorded_at.isoformat(),
        }


@dataclass(slots=True)
class RunbookStepRecorder:
    """Process-local audit buffer for runbook-step events."""

    capacity: int = DEFAULT_RECORDER_CAPACITY
    _events: deque[RunbookStepEvent] = field(default_factory=deque)

    def append(self, event: RunbookStepEvent) -> None:
        self._events.append(event)
        while len(self._events) > self.capacity:
            self._events.popleft()

    def recent(self, *, workspace_id: uuid.UUID, limit: int = 50) -> list[RunbookStepEvent]:
        """Return up to ``limit`` most-recent events for ``workspace_id``."""
        out: list[RunbookStepEvent] = []
        for event in reversed(self._events):
            if event.workspace_id != workspace_id:
                continue
            out.append(event)
            if len(out) >= limit:
                break
        return out


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def validate_alert_id(alert_id: str) -> None:
    """Raise ``ValueError("invalid_alert_id:...")`` on malformed ids."""
    if not isinstance(alert_id, str) or not _ALERT_ID_PATTERN.match(alert_id):
        raise ValueError(f"{INVALID_ALERT_ID_CODE}:{INVALID_ALERT_ID_MESSAGE}")


def validate_runbook_name(runbook_name: str) -> None:
    """Raise ``ValueError("invalid_runbook_name:...")`` on malformed names."""
    if not isinstance(runbook_name, str) or not _RUNBOOK_NAME_PATTERN.match(runbook_name):
        raise ValueError(f"{INVALID_RUNBOOK_NAME_CODE}:{INVALID_RUNBOOK_NAME_MESSAGE}")


def validate_step_id(step_id: str) -> None:
    """Raise ``ValueError("invalid_step_id:...")`` on malformed ids."""
    if not isinstance(step_id, str) or not _STEP_ID_PATTERN.match(step_id):
        raise ValueError(f"{INVALID_STEP_ID_CODE}:step_id must match [A-Za-z0-9._-]+")


# ---------------------------------------------------------------------------
# Runbook loading
# ---------------------------------------------------------------------------


def runbook_from_entity_metadata(entity: EntityNodeView) -> RunbookDocument | None:
    """Reconstruct a :class:`RunbookDocument` from an indexed entity."""
    text = entity.chunk_text or ""
    if not text or text[0] not in "{":
        return None
    try:
        return RunbookDocument.model_validate_json(text)
    except ValueError:
        return None


def alert_view_from_entity(entity: EntityNodeView) -> AlertView:
    """Project an alert entity into the matcher's input shape."""
    tags: list[str] = []
    severity: str | None = None
    text = entity.chunk_text or ""
    if text.startswith("{"):
        try:
            import json as _json

            blob = _json.loads(text)
            raw_tags = blob.get("tags")
            if isinstance(raw_tags, list):
                tags = [str(t) for t in raw_tags if t is not None]
            raw_sev = blob.get("severity")
            if isinstance(raw_sev, str):
                severity = raw_sev
        except (ValueError, AttributeError):
            pass
    return AlertView(name=entity.name, tags=tags, severity=severity)


# ---------------------------------------------------------------------------
# Suggestion scoring — pure logic
# ---------------------------------------------------------------------------


def score_runbook_candidates(
    *,
    alert_view: AlertView,
    runbooks: Iterable[RunbookDocument],
    top_k: int,
) -> list[MatchResult]:
    """Score ``runbooks`` against ``alert_view`` and return the top ``top_k``.

    Returns an empty list when no candidate has confidence > 0.  Stable
    ordering: confidence DESC, then runbook name ASC for determinism when
    scores tie.
    """
    scored: list[MatchResult] = []
    for runbook in runbooks:
        result = score_runbook_against_alert(runbook=runbook, alert=alert_view)
        if result.confidence > 0.0:
            scored.append(result)
    scored.sort(key=lambda r: (-r.confidence, r.runbook_name))
    return scored[:top_k]


def citation_for(runbook: RunbookDocument) -> dict[str, Any]:
    """Build the citation block surfaced alongside each suggestion."""
    first_step: dict[str, Any] | None = None
    if runbook.steps:
        first = runbook.steps[0]
        first_step = {
            "step_id": first.step_id,
            "heading": first.heading,
            "heading_level": first.heading_level,
        }
    return {
        "runbook_name": runbook.canonical_name,
        "title": runbook.title,
        "source_uid": runbook.source_uid,
        "rel_path": runbook.rel_path,
        "tags": runbook.front_matter.tags,
        "severity": runbook.front_matter.severity,
        "first_step": first_step,
        "parse_warnings": runbook.parse_warnings,
    }


# ---------------------------------------------------------------------------
# Step-name helpers
# ---------------------------------------------------------------------------


def runbook_source_uid(runbook_name: str) -> str:
    """Extract the ``source_uid`` segment from a canonical runbook name."""
    tail = runbook_name[len("runbook://") :]
    head, _, _ = tail.partition("/")
    return head


def runbook_rel_path(runbook_name: str) -> str:
    """Extract the ``rel_path`` tail from a canonical runbook name."""
    tail = runbook_name[len("runbook://") :]
    _, _, rest = tail.partition("/")
    return rest


def make_runbook_step_event(
    *,
    workspace_id: uuid.UUID,
    incident_id: str,
    alert_id: str,
    runbook_name: str,
    step_id: str,
    outcome: str = "executed",
    actor: str | None = None,
    note: str | None = None,
    valid_from: datetime | None = None,
    enforce_utc_fn: Any,  # Callable[[datetime | None], datetime | None]
) -> RunbookStepEvent:
    """Build a :class:`RunbookStepEvent` after validation.

    ``enforce_utc_fn`` is injected by the caller (``apps/server``) to
    keep this module free of the ``omniscience_server.as_of`` import.
    Pass ``omniscience_server.as_of.enforce_utc`` or any callable with
    the same signature.
    """
    validate_alert_id(alert_id)
    validate_runbook_name(runbook_name)
    validate_step_id(step_id)
    if not incident_id or not isinstance(incident_id, str):
        raise ValueError("invalid_incident_id:incident_id must be a non-empty string")

    now = datetime.now(UTC)
    valid_from_norm = enforce_utc_fn(valid_from) or now

    step_name = canonical_runbook_step_name(
        runbook_source_uid(runbook_name),
        runbook_rel_path(runbook_name),
        step_id,
    )

    return RunbookStepEvent(
        event_id=uuid.uuid4(),
        workspace_id=workspace_id,
        alert_id=alert_id,
        incident_id=incident_id,
        runbook_name=runbook_name,
        step_id=step_id,
        step_name=step_name,
        actor=actor,
        note=note,
        outcome=outcome,
        valid_from=valid_from_norm,
        valid_to=None,
        recorded_at=now,
    )


__all__ = [
    "ALERT_NOT_FOUND_CODE",
    "DEFAULT_RECORDER_CAPACITY",
    "DEFAULT_TOP_K",
    "INVALID_ALERT_ID_CODE",
    "INVALID_ALERT_ID_MESSAGE",
    "INVALID_RUNBOOK_NAME_CODE",
    "INVALID_RUNBOOK_NAME_MESSAGE",
    "INVALID_STEP_ID_CODE",
    "MAX_TOP_K",
    "RUNBOOK_NOT_FOUND_CODE",
    "RUNBOOK_STEP_EVENTS_TOTAL",
    "RUNBOOK_SUGGEST_TOTAL",
    "RunbookStepEvent",
    "RunbookStepRecorder",
    "alert_view_from_entity",
    "citation_for",
    "make_runbook_step_event",
    "runbook_from_entity_metadata",
    "runbook_rel_path",
    "runbook_source_uid",
    "score_runbook_candidates",
    "validate_alert_id",
    "validate_runbook_name",
    "validate_step_id",
]
