"""Runbook suggest + step-recording (issue #231).

Composes the two server-side primitives the runbook epic needs:

1. :func:`suggest_runbook` — given an alert id, returns the top N matching
   runbooks with their match traces.  Backed by
   :func:`omniscience_connectors.runbook.linker.score_runbook_against_alert`.

2. :func:`record_runbook_step` — records that a responder executed a
   specific step from a specific runbook against an incident.  The event
   is appended to the in-memory :class:`RunbookStepRecorder` (the
   bitemporal-graph audit surface) AND a structured log line + Prometheus
   counter are emitted so the audit trail is queryable from three
   independent vantage points.

Why an in-memory recorder (and not a DB table)
-----------------------------------------------
A persistent ``runbook_step_event`` SQL table would require an Alembic
migration touching :mod:`omniscience_core.db.models` — out of scope for
issue #231 under the file-ownership map.  The recorder is a small
process-local ring buffer that:

* Surfaces the event on ``app.state.runbook_step_recorder.recent(...)``
  for the integration test and the admin UI.
* Logs every event at INFO with the full bitemporal triple
  (``valid_from``, ``recorded_at``) so log-shippers persist it.
* Increments :data:`RUNBOOK_STEP_EVENTS_TOTAL` so Prometheus retains the
  count even after the buffer rolls over.

A future PR (tracked under epic #230 "incident workflow depth") will
promote the recorder to a proper SQL table; the public function
signatures defined here are deliberately stable so that migration is
additive.
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
from fastapi import FastAPI
from omniscience_connectors.runbook.linker import (
    AlertView,
    MatchResult,
    score_runbook_against_alert,
)
from omniscience_connectors.runbook.models import (
    RunbookDocument,
    canonical_runbook_step_name,
)
from omniscience_core.storage import EntityNodeView, GraphStore
from prometheus_client import Counter

from omniscience_server.as_of import (
    enforce_utc,
    record_request_duration,
    resolve_effective_as_of,
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Error codes (mirrored on the MCP / REST surfaces — same envelope shape
# the resolve_incident handlers use).
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

# Soft cap on the in-memory event buffer.  Past this limit the oldest
# event is dropped (FIFO); Prometheus retains the count regardless.
DEFAULT_RECORDER_CAPACITY: Final[int] = 10_000

# ---------------------------------------------------------------------------
# Validation regexes — kept narrow so error messages are precise.
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
    """A single recorded runbook-step execution.

    The triple ``(valid_from, valid_to, recorded_at)`` follows the
    ADR-0008 bitemporal convention so the event slots into the graph
    when a future migration promotes it to a persistent table.
    """

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
    """Process-local audit buffer for runbook-step events.

    The buffer is bounded (FIFO) so a long-running process does not leak
    memory.  Tests assert against :meth:`recent`; production observability
    leans on the structured-log and Prometheus surfaces, which are
    durable independently of this buffer.
    """

    capacity: int = DEFAULT_RECORDER_CAPACITY
    _events: deque[RunbookStepEvent] = field(default_factory=deque)

    def append(self, event: RunbookStepEvent) -> None:
        self._events.append(event)
        while len(self._events) > self.capacity:
            self._events.popleft()

    def recent(self, *, workspace_id: uuid.UUID, limit: int = 50) -> list[RunbookStepEvent]:
        """Return up to ``limit`` most-recent events for ``workspace_id``.

        Workspace filtering is honoured here so test assertions match the
        ACL contract; the Prometheus counter is the only cross-workspace
        view of the data and it's an aggregate count, not a leak.
        """
        out: list[RunbookStepEvent] = []
        # Iterate from newest to oldest.
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
    """Raise ``ValueError("invalid_step_id:...")`` on malformed ids.

    Step ids are slugified heading text — alphanumerics, dot, underscore,
    dash.  See :func:`omniscience_connectors.runbook.models._slugify`.
    """
    if not isinstance(step_id, str) or not _STEP_ID_PATTERN.match(step_id):
        raise ValueError(f"{INVALID_STEP_ID_CODE}:step_id must match [A-Za-z0-9._-]+")


# ---------------------------------------------------------------------------
# Runbook loading
# ---------------------------------------------------------------------------


def runbook_from_entity_metadata(entity: EntityNodeView) -> RunbookDocument | None:
    """Reconstruct a :class:`RunbookDocument` from an indexed entity.

    The runbook connector stores the parsed JSON payload on
    :attr:`EntityNodeView.chunk_text`.  Adapters that serialise metadata
    differently can populate the chunk_text slot equivalently — the
    cross-adapter contract is "chunk_text is a JSON-serialised
    RunbookDocument".

    Returns ``None`` when the entity does not carry a parseable runbook
    payload (legacy / non-runbook entities, malformed JSON).  Callers
    treat ``None`` as "skip" — the matcher cannot score this candidate.
    """
    text = entity.chunk_text or ""
    if not text or text[0] not in "{":
        return None
    try:
        return RunbookDocument.model_validate_json(text)
    except ValueError:
        return None


def _alert_view_from_entity(entity: EntityNodeView) -> AlertView:
    """Project an alert entity into the matcher's input shape.

    Alert connectors store the normalised payload on ``chunk_text`` (see
    :mod:`omniscience_connectors.alerts`); when that payload includes
    ``tags`` / ``severity`` we surface them so the matcher can score
    them.  Missing fields silently default to empty — a tag-less alert
    just loses the 0.15 contribution from tag overlap.
    """
    tags: list[str] = []
    severity: str | None = None
    # The alert payload may be JSON-stringified or plain text; we read
    # the obvious slots conservatively.  Failure here is silent — the
    # matcher tolerates an empty AlertView.
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
# Public entry points
# ---------------------------------------------------------------------------


async def suggest_runbook(
    *,
    app: FastAPI,
    alert_id: str,
    workspace_id: uuid.UUID,
    as_of: datetime | None = None,
    top_k: int = DEFAULT_TOP_K,
    candidates: Iterable[RunbookDocument] | None = None,
) -> dict[str, Any]:
    """Return the top ``top_k`` matching runbooks for ``alert_id``.

    Parameters
    ----------
    app:
        FastAPI app exposing ``app.state.graph_store``.  When ``candidates``
        is supplied (tests, deterministic replays) the graph_store is
        consulted only for the alert seed; otherwise the handler walks
        ``find_related(edge_types=["MATCHES", "TARGETS"])`` to gather the
        runbook neighbourhood.
    alert_id:
        Canonical alert URI ``alert://{provider}/{provider_alert_id}``.
    workspace_id:
        REQUIRED.  Every graph read carries it (ACL invariant).
    as_of:
        Optional ADR-0008 §5 bitemporal anchor.
    top_k:
        Number of runbooks to return.  Clamped to ``[1, MAX_TOP_K]``.
    candidates:
        Optional iterable of pre-loaded :class:`RunbookDocument` to score
        against.  Used by the integration test so it does not need a
        graph backend with runbook-entity round-trip support.
    """
    validate_alert_id(alert_id)
    normalised_as_of = enforce_utc(as_of)
    clamped_top_k = max(1, min(top_k, MAX_TOP_K))

    graph_store: GraphStore | None = getattr(app.state, "graph_store", None)
    if graph_store is None and candidates is None:
        raise RuntimeError("graph_store not available on app.state and no candidates supplied")

    with record_request_duration(surface="mcp", tool="suggest_runbook", as_of=normalised_as_of):
        alert_view, runbooks = await _load_alert_and_candidates(
            graph_store=graph_store,
            alert_id=alert_id,
            workspace_id=workspace_id,
            as_of=normalised_as_of,
            preloaded=candidates,
        )

    if alert_view is None:
        RUNBOOK_SUGGEST_TOTAL.labels(outcome="alert_not_found").inc()
        raise ValueError(f"{ALERT_NOT_FOUND_CODE}:{alert_id}")

    scored: list[MatchResult] = []
    for runbook in runbooks:
        result = score_runbook_against_alert(runbook=runbook, alert=alert_view)
        if result.confidence > 0.0:
            scored.append(result)

    # Stable ordering: confidence DESC, then runbook name ASC for
    # determinism when scores tie.
    scored.sort(key=lambda r: (-r.confidence, r.runbook_name))
    top = scored[:clamped_top_k]

    outcome = "match" if top else "no_match"
    RUNBOOK_SUGGEST_TOTAL.labels(outcome=outcome).inc()

    top_names = {r.runbook_name for r in top}
    citations = [
        _citation_for(runbook) for runbook in runbooks if runbook.canonical_name in top_names
    ]
    citations_by_name = {c["runbook_name"]: c for c in citations}

    return {
        "alert_id": alert_id,
        "effective_as_of": resolve_effective_as_of(normalised_as_of).isoformat(),
        "suggestions": [
            {
                **match.to_payload(),
                "citation": citations_by_name.get(match.runbook_name),
            }
            for match in top
        ],
        "meta": {
            "candidate_count": len(runbooks),
            "scored_count": len(scored),
            "top_k": clamped_top_k,
            "alert_tag_count": len(alert_view.tags),
        },
    }


async def _load_alert_and_candidates(
    *,
    graph_store: GraphStore | None,
    alert_id: str,
    workspace_id: uuid.UUID,
    as_of: datetime | None,
    preloaded: Iterable[RunbookDocument] | None,
) -> tuple[AlertView | None, list[RunbookDocument]]:
    """Resolve the alert seed + the runbook candidate set.

    When ``preloaded`` is supplied (integration test) we still hit
    ``get_entity`` so the ACL invariant is exercised (and the test
    asserts the call).  When ``preloaded`` is ``None`` we walk
    ``find_related`` and rebuild :class:`RunbookDocument` instances from
    neighbour entities whose chunk_text is a serialised runbook payload.
    """
    if graph_store is None:
        # Should never happen — the caller guards on it.  Defensive.
        return None, list(preloaded or [])

    alert_entity = await graph_store.get_entity(
        entity_name=alert_id,
        workspace_id=workspace_id,
        as_of=as_of,
    )
    if alert_entity is None:
        return None, []
    alert_view = _alert_view_from_entity(alert_entity)

    if preloaded is not None:
        return alert_view, list(preloaded)

    # Walk the graph for runbook neighbours.  Depth 1 covers direct
    # ``MATCHES`` edges; depth 2 covers ``alert -> service -> runbook``
    # patterns.  We do not request specific edge_types so the adapter
    # surfaces every related entity; we filter by kind here.
    related = await graph_store.find_related(
        entity_name=alert_id,
        workspace_id=workspace_id,
        as_of=as_of,
        max_depth=2,
    )

    candidates: list[RunbookDocument] = []
    for neighbour in related.related:
        if neighbour.kind != "runbook":
            continue
        rb = runbook_from_entity_metadata(neighbour)
        if rb is not None:
            candidates.append(rb)
    return alert_view, candidates


def _citation_for(runbook: RunbookDocument) -> dict[str, Any]:
    """Build the citation block surfaced alongside each suggestion.

    Pulls the runbook title + first heading body so the responder UI can
    render a preview without a second round-trip.
    """
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
# record_runbook_step
# ---------------------------------------------------------------------------


def get_recorder(app: FastAPI) -> RunbookStepRecorder:
    """Return (lazily-creating) the process-local recorder on app.state."""
    recorder = getattr(app.state, "runbook_step_recorder", None)
    if recorder is None:
        recorder = RunbookStepRecorder()
        app.state.runbook_step_recorder = recorder
    if not isinstance(recorder, RunbookStepRecorder):  # pragma: no cover - defensive
        raise RuntimeError("app.state.runbook_step_recorder is not a RunbookStepRecorder")
    return recorder


def record_runbook_step(
    *,
    app: FastAPI,
    workspace_id: uuid.UUID,
    incident_id: str,
    alert_id: str,
    runbook_name: str,
    step_id: str,
    outcome: str = "executed",
    actor: str | None = None,
    note: str | None = None,
    valid_from: datetime | None = None,
) -> RunbookStepEvent:
    """Record a single runbook-step execution as a bitemporal event.

    Three durability surfaces (any one is sufficient to reconstruct the
    history; we write to all three for defence-in-depth):

    * The in-memory :class:`RunbookStepRecorder` ring buffer.
    * A structured log line (event ``"runbook_step_executed"``).
    * The :data:`RUNBOOK_STEP_EVENTS_TOTAL` Prometheus counter.

    ``valid_from`` defaults to "now"; callers replaying a historical
    execution may supply an earlier timestamp.  ``recorded_at`` is always
    "now" — the bitemporal contract distinguishes "when it happened" from
    "when we learned it happened".
    """
    validate_alert_id(alert_id)
    validate_runbook_name(runbook_name)
    validate_step_id(step_id)
    if not incident_id or not isinstance(incident_id, str):
        raise ValueError("invalid_incident_id:incident_id must be a non-empty string")

    now = datetime.now(UTC)
    valid_from_norm = enforce_utc(valid_from) or now

    step_name = canonical_runbook_step_name(
        # The recorder is downstream of the source_uid -> rel_path split
        # encoded in the canonical name.  We pass the slugged values
        # straight through to canonical_runbook_step_name so the result
        # is byte-identical to the connector-emitted entity name.
        _runbook_source_uid(runbook_name),
        _runbook_rel_path(runbook_name),
        step_id,
    )

    event = RunbookStepEvent(
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

    get_recorder(app).append(event)
    RUNBOOK_STEP_EVENTS_TOTAL.labels(workspace_prefix=str(workspace_id)[:8]).inc()
    log.info(
        "runbook_step_executed",
        event_id=str(event.event_id),
        workspace_id=str(workspace_id),
        incident_id=incident_id,
        alert_id=alert_id,
        runbook_name=runbook_name,
        step_id=step_id,
        step_name=step_name,
        outcome=outcome,
        valid_from=event.valid_from.isoformat(),
        recorded_at=event.recorded_at.isoformat(),
    )
    return event


def _runbook_source_uid(runbook_name: str) -> str:
    """Extract the ``source_uid`` segment from a canonical runbook name."""
    # ``runbook://{source_uid}/{rel_path...}`` — split off the scheme and
    # take everything up to the first slash.
    tail = runbook_name[len("runbook://") :]
    head, _, _ = tail.partition("/")
    return head


def _runbook_rel_path(runbook_name: str) -> str:
    """Extract the ``rel_path`` tail from a canonical runbook name."""
    tail = runbook_name[len("runbook://") :]
    _, _, rest = tail.partition("/")
    return rest


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
    "get_recorder",
    "record_runbook_step",
    "runbook_from_entity_metadata",
    "suggest_runbook",
    "validate_alert_id",
    "validate_runbook_name",
    "validate_step_id",
]
