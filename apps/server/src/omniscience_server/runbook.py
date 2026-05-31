"""Runbook orchestration layer — suggest + step-recording (issue #231).

FastAPI wiring layer for the runbook MCP/REST tools.  Pure domain logic
(types, validators, scoring, step-event building) lives in
:mod:`omniscience_retrieval.runbook`; this module wires them to
``app.state.graph_store`` and the structured-log / Prometheus surfaces.

Why an in-memory recorder (and not a DB table)
-----------------------------------------------
See :mod:`omniscience_retrieval.runbook` for rationale and the stable
public signatures a future migration can promote to SQL.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime
from typing import Any

import structlog
from fastapi import FastAPI
from omniscience_connectors.runbook.models import RunbookDocument
from omniscience_core.storage import GraphStore

# Re-export domain surface for backward-compatible imports from other
# modules in apps/server (REST handlers, MCP server, etc.).
from omniscience_retrieval.runbook import (
    ALERT_NOT_FOUND_CODE,
    DEFAULT_RECORDER_CAPACITY,
    DEFAULT_TOP_K,
    INVALID_ALERT_ID_CODE,
    INVALID_ALERT_ID_MESSAGE,
    INVALID_RUNBOOK_NAME_CODE,
    INVALID_RUNBOOK_NAME_MESSAGE,
    INVALID_STEP_ID_CODE,
    MAX_TOP_K,
    RUNBOOK_NOT_FOUND_CODE,
    RUNBOOK_STEP_EVENTS_TOTAL,
    RUNBOOK_SUGGEST_TOTAL,
    RunbookStepEvent,
    RunbookStepRecorder,
    alert_view_from_entity,
    runbook_from_entity_metadata,
    validate_alert_id,
    validate_runbook_name,
    validate_step_id,
)
from omniscience_retrieval.runbook import (
    citation_for as _citation_for,
)
from omniscience_retrieval.runbook import (
    make_runbook_step_event as _make_runbook_step_event,
)
from omniscience_retrieval.runbook import (
    score_runbook_candidates as _score_runbook_candidates,
)

from omniscience_server.as_of import (
    enforce_utc,
    record_request_duration,
    resolve_effective_as_of,
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# App-state helper
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
    """Return the top ``top_k`` matching runbooks for ``alert_id``."""
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

    top = _score_runbook_candidates(
        alert_view=alert_view,
        runbooks=runbooks,
        top_k=clamped_top_k,
    )

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
            "scored_count": len(top),
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
) -> tuple[Any | None, list[RunbookDocument]]:
    """Resolve the alert seed + the runbook candidate set."""
    if graph_store is None:
        return None, list(preloaded or [])

    alert_entity = await graph_store.get_entity(
        entity_name=alert_id,
        workspace_id=workspace_id,
        as_of=as_of,
    )
    if alert_entity is None:
        return None, []
    alert_view = alert_view_from_entity(alert_entity)

    if preloaded is not None:
        return alert_view, list(preloaded)

    related = await graph_store.find_related(
        entity_name=alert_id,
        workspace_id=workspace_id,
        as_of=as_of,
        max_depth=2,
    )

    runbooks: list[RunbookDocument] = []
    for neighbour in related.related:
        if neighbour.kind != "runbook":
            continue
        rb = runbook_from_entity_metadata(neighbour)
        if rb is not None:
            runbooks.append(rb)
    return alert_view, runbooks


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
    """Record a single runbook-step execution as a bitemporal event."""
    event = _make_runbook_step_event(
        workspace_id=workspace_id,
        incident_id=incident_id,
        alert_id=alert_id,
        runbook_name=runbook_name,
        step_id=step_id,
        outcome=outcome,
        actor=actor,
        note=note,
        valid_from=valid_from,
        enforce_utc_fn=enforce_utc,
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
        step_name=event.step_name,
        outcome=outcome,
        valid_from=event.valid_from.isoformat(),
        recorded_at=event.recorded_at.isoformat(),
    )
    return event


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
