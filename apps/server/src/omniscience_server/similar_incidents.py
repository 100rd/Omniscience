"""Same-incident clustering orchestration layer (issue #233, Wave 2 of epic #230).

FastAPI wiring layer for the ``find_similar_incidents`` MCP/REST tool.
Pure domain logic lives in :mod:`omniscience_retrieval.incidents.similar`;
this module composes ``GraphStore`` I/O, embedding provider access, and
the ranking pipeline.

ACL invariant (non-negotiable)
------------------------------

- ``workspace_id`` is taken from the caller's bearer token; every
  ``GraphStore`` call carries it.  Cross-workspace seeds return
  ``alert_not_found`` (issue #117 / ADR-0005).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import FastAPI
from omniscience_core.storage import GraphStore
from omniscience_embeddings.base import EmbeddingProvider
from omniscience_index.clustering import DEFAULT_LIMIT, DEFAULT_SINCE_DAYS
from omniscience_retrieval.incidents.resolution import (
    ALERT_NOT_FOUND_CODE,
)
from omniscience_retrieval.incidents.resolution import (
    validate_alert_id as _validate_alert_id,
)

# Re-export public domain types and constants for backward compatibility.
from omniscience_retrieval.incidents.similar import (
    MAX_LIMIT,
    MAX_SINCE_DAYS,
    MIN_LIMIT,
    MIN_SINCE_DAYS,
    FindSimilarIncidentsResponse,
    SimilarIncident,
    SimilarIncidentRequest,
)
from omniscience_retrieval.incidents.similar import (
    SeededContext as _SeededContext,
)
from omniscience_retrieval.incidents.similar import (
    build_features as _build_features,
)
from omniscience_retrieval.incidents.similar import (
    build_similar_response as _build_similar_response,
)
from omniscience_retrieval.incidents.similar import (
    discover_candidates as _discover_candidates,
)
from omniscience_retrieval.incidents.similar import (
    rank_similar_incidents as _rank_similar_incidents,
)

from omniscience_server.as_of import (
    enforce_utc,
    record_request_duration,
    resolve_effective_as_of,
)

log = structlog.get_logger(__name__)


async def mcp_find_similar_incidents(
    app: FastAPI,
    *,
    incident_id: str,
    workspace_id: uuid.UUID,
    limit: int = DEFAULT_LIMIT,
    since_days: int = DEFAULT_SINCE_DAYS,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Return ranked similar past incidents for the given seed.

    Mirrors the shape of :func:`mcp_resolve_incident` — same ACL
    invariant, same ``as_of`` plumbing, same telemetry histogram.
    """
    _validate_alert_id(incident_id)
    normalised_as_of = enforce_utc(as_of)
    clamped_limit = max(MIN_LIMIT, min(limit, MAX_LIMIT))
    clamped_since = max(MIN_SINCE_DAYS, min(since_days, MAX_SINCE_DAYS))

    graph_store: GraphStore | None = getattr(app.state, "graph_store", None)
    if graph_store is None:
        raise RuntimeError("graph_store not available on app.state")
    embedder: EmbeddingProvider | None = getattr(app.state, "embedding_provider", None)

    with record_request_duration(
        surface="mcp", tool="find_similar_incidents", as_of=normalised_as_of
    ) as patcher:
        seed = await graph_store.get_entity(
            entity_name=incident_id,
            workspace_id=workspace_id,
            as_of=normalised_as_of,
        )
        if seed is None:
            if normalised_as_of is not None:
                patcher.mark_pre_history()
            raise ValueError(f"{ALERT_NOT_FOUND_CODE}:{incident_id}")

        now = datetime.now(tz=UTC)
        try:
            context = await _discover_candidates(
                graph_store=graph_store,
                seed=seed,
                workspace_id=workspace_id,
                as_of=normalised_as_of,
                since_days=clamped_since,
                now=now,
            )
        except ValueError as exc:
            if str(exc).startswith("entity_not_found:"):
                context = _SeededContext(seed=seed, seed_service=None, candidates=[])
            else:
                raise

        seed_features, candidate_features = await _build_features(
            embedder=embedder,
            seed=seed,
            seed_service=context.seed_service,
            candidates=context.candidates,
        )

        matches = _rank_similar_incidents(
            seed_features=seed_features,
            candidate_features=candidate_features,
            candidates=context.candidates,
            now=now,
            since_days=clamped_since,
            limit=clamped_limit,
        )

        response = _build_similar_response(
            incident_id=incident_id,
            seed_features=seed_features,
            matches=matches,
            effective_as_of=resolve_effective_as_of(normalised_as_of),
        )
        return response.model_dump(mode="json")


async def collect_similar_past(
    app: FastAPI,
    *,
    incident_id: str,
    workspace_id: uuid.UUID,
    as_of: datetime | None,
    limit: int = DEFAULT_LIMIT,
    since_days: int = DEFAULT_SINCE_DAYS,
) -> list[dict[str, Any]]:
    """Return the ``matches`` portion of the similar-incidents response.

    Used by :func:`omniscience_server.incidents.mcp_resolve_incident` to
    populate its new ``similar_past`` field.  Failures are swallowed and
    logged — the augmentation is best-effort.
    """
    try:
        payload = await mcp_find_similar_incidents(
            app=app,
            incident_id=incident_id,
            workspace_id=workspace_id,
            limit=limit,
            since_days=since_days,
            as_of=as_of,
        )
    except ValueError:
        return []
    except Exception as exc:  # pragma: no cover - defensive
        log.warning(
            "resolve_incident_similar_past_failed",
            incident_id=incident_id,
            error=str(exc),
        )
        return []
    matches = payload.get("matches", [])
    if not isinstance(matches, list):
        return []
    return [m for m in matches if isinstance(m, dict)]


__all__ = [
    "MAX_LIMIT",
    "MAX_SINCE_DAYS",
    "MIN_LIMIT",
    "MIN_SINCE_DAYS",
    "FindSimilarIncidentsResponse",
    "SimilarIncident",
    "SimilarIncidentRequest",
    "collect_similar_past",
    "mcp_find_similar_incidents",
]
