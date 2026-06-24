"""resolve_incident orchestration layer (issue #153, Wave 2 of epic #99).

This module is the FastAPI-wiring layer for the ``resolve_incident`` MCP/REST
tool.  Pure domain and analysis logic lives in
:mod:`omniscience_retrieval.incidents`; this module composes the
``GraphStore`` I/O, scoring-config loading, and ``similar_past``
augmentation with the domain primitives.

ACL invariant (non-negotiable)
------------------------------

- ``workspace_id`` is taken from the caller's bearer token, never from
  input.  Every store call carries it (see :meth:`GraphStore` ACL
  contract from #117 / ADR-0005).
- A foreign workspace's ``alert_id`` returns ``alert_not_found`` — the
  same response a non-existent alert would produce.

``as_of`` deferred from #173
----------------------------

See :mod:`omniscience_server.as_of` for bitemporal handling.

AP4 changes
-----------

- ``support_size`` is no longer hardcoded to ``0`` ("cold start assumed").
  It defaults to :data:`~omniscience_retrieval.probabilistic_scoring.DEFAULT_SUPPORT_SIZE`
  (``30``), which allows the isotonic path when a fitted artifact is
  loaded.  Callers can override via the ``support_size`` parameter when
  real observation counts are available.
- The response now carries an explicit ``calibrated: bool`` field.  It is
  ``True`` when the fitted isotonic artifact is in use (loaded from disk),
  ``False`` otherwise.  This is separate from ``uncalibrated`` (which
  flags provisional / low-support scoring).  Both fields are surfaced to
  MCP and REST consumers.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import structlog
from fastapi import FastAPI
from omniscience_core.storage import GraphResultView, GraphStore

# ---------------------------------------------------------------------------
# Re-export the public API that external importers (tests, REST, MCP)
# relied on from this module's original location.
# ---------------------------------------------------------------------------
from omniscience_retrieval.incidents.resolution import (
    ALERT_NOT_FOUND_CODE,
    CONFIDENCE_ALERT_ONLY,
    CONFIDENCE_NONE,
    CONFIDENCE_PR_NO_TEMPORAL_MATCH,
    CONFIDENCE_PR_TEMPORAL_MATCH,
    CONFIDENCE_RESOURCE_ONLY,
    DEFAULT_MAX_DEPTH,
    INVALID_ALERT_ID_CODE,
    INVALID_ALERT_ID_MESSAGE,
    MAX_MAX_DEPTH,
    MAX_SLACK_THREADS_RETURNED,
    MIN_MAX_DEPTH,
    PR_RECENCY_WINDOW_SECONDS,
    AlertIdRequest,
    AlertSummary,
    PrSummary,
    ResolveIncidentResponse,
    ResourceSummary,
    SlackThreadSummary,
)
from omniscience_retrieval.incidents.resolution import (
    build_meta as _build_meta,
)
from omniscience_retrieval.incidents.resolution import (
    build_resolve_response as _build_response,
)
from omniscience_retrieval.incidents.resolution import (
    classify_neighbours as _classify_neighbours,
)
from omniscience_retrieval.incidents.resolution import (
    score_incident as _score_incident,
)
from omniscience_retrieval.incidents.resolution import (
    validate_alert_id as _validate_alert_id,
)
from omniscience_retrieval.incidents.scoring import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    IncidentScoringConfig,
    load_workspace_config,
)
from omniscience_retrieval.probabilistic_scoring import (
    DEFAULT_SUPPORT_SIZE,
    is_fitted_map_loaded,
)

from omniscience_server.as_of import (
    enforce_utc,
    record_request_duration,
    resolve_effective_as_of,
)

log = structlog.get_logger(__name__)

# Public re-export so existing importers of
# ``omniscience_server.incidents._validate_alert_id`` keep working.
validate_alert_id = _validate_alert_id
_ALERT_PREFIX = "alert://"


# ---------------------------------------------------------------------------
# Public composition entry point
# ---------------------------------------------------------------------------


async def mcp_resolve_incident(
    app: FastAPI,
    alert_id: str,
    workspace_id: uuid.UUID,
    as_of: datetime | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    support_size: int | None = None,
) -> dict[str, Any]:
    """Resolve an alert into a recommendation bundle.

    Mirrors the shape of :func:`mcp_get_related_entities` (issue #153
    requirement) — same ``workspace_id`` ACL invariant, same ``as_of``
    plumbing, same telemetry histogram.

    Parameters
    ----------
    app:
        FastAPI app exposing ``app.state.graph_store``.
    alert_id:
        Canonical alert URI ``alert://{provider}/{provider_alert_id}``.
    workspace_id:
        REQUIRED. Forwarded to every ``GraphStore`` call.
    as_of:
        Optional ADR-0008 §5 bitemporal anchor.
    max_depth:
        BFS depth from the alert seed.  Clamped to
        ``[MIN_MAX_DEPTH, MAX_MAX_DEPTH]``; default ``DEFAULT_MAX_DEPTH``.
    support_size:
        Number of labeled examples available for the isotonic calibrator.
        ``None`` uses :data:`~omniscience_retrieval.probabilistic_scoring.DEFAULT_SUPPORT_SIZE`
        (``30``), enabling the isotonic path when a fitted artifact is loaded.
        Pass ``0`` explicitly only to force Platt-only (e.g. during cold start
        before any labeled data is collected).

    AP4 response contract
    ---------------------

    The returned dict includes:
    - ``resolution_confidence``: calibrated float in ``[0, 1]``.
    - ``calibrated``: ``True`` when the fitted isotonic artifact is loaded,
      ``False`` otherwise (cold start / no artifact yet).
    - ``uncalibrated``: ``True`` when the scoring is provisional (low support
      or any entity is parked).
    """
    _validate_alert_id(alert_id)
    normalised_as_of = enforce_utc(as_of)
    clamped_depth = max(MIN_MAX_DEPTH, min(max_depth, MAX_MAX_DEPTH))

    graph_store: GraphStore | None = getattr(app.state, "graph_store", None)
    if graph_store is None:
        raise RuntimeError("graph_store not available on app.state")

    # AP4: determine effective support_size — never silently default to 0.
    effective_support = support_size if support_size is not None else DEFAULT_SUPPORT_SIZE

    with record_request_duration(
        surface="mcp", tool="resolve_incident", as_of=normalised_as_of
    ) as patcher:
        seed = await graph_store.get_entity(
            entity_name=alert_id,
            workspace_id=workspace_id,
            as_of=normalised_as_of,
        )
        if seed is None:
            if normalised_as_of is not None:
                patcher.mark_pre_history()
            raise ValueError(f"{ALERT_NOT_FOUND_CODE}:{alert_id}")

        try:
            graph_result = await graph_store.find_related(
                entity_name=alert_id,
                workspace_id=workspace_id,
                as_of=normalised_as_of,
                max_depth=clamped_depth,
            )
        except ValueError as exc:
            if str(exc).startswith("entity_not_found:"):
                graph_result = GraphResultView(seed=seed, related=[], edges=[])
            else:
                raise

        outbox_consumer = getattr(app.state, "outbox_consumer", None)
        if outbox_consumer and hasattr(outbox_consumer, "_parked_entities"):
            parked = outbox_consumer._parked_entities
            if seed.id in parked:
                seed.is_parked = True
            for node in graph_result.related:
                if node.id in parked:
                    node.is_parked = True

        classified = _classify_neighbours(graph_result)
        scoring_config = await _load_scoring_config(app, workspace_id)
        confidence, is_provisional = _score_incident(
            alert=seed,
            classified=classified,
            max_depth=clamped_depth,
            config=scoring_config,
            as_of=normalised_as_of,
            support_size=effective_support,
        )

        # AP4: flag whether the returned confidence number is backed by a
        # fitted isotonic artifact (calibrated=True) or is Platt-only fallback.
        is_calibrated: bool = is_fitted_map_loaded()

        meta = _build_meta(
            confidence=confidence, config=scoring_config, is_provisional=is_provisional
        )
        similar_past = await _augment_with_similar_past(
            app=app,
            alert_id=alert_id,
            workspace_id=workspace_id,
            as_of=normalised_as_of,
        )
        response = _build_response(
            alert=seed,
            classified=classified,
            resolution_confidence=confidence,
            evidence_recency=1.0,
            inferential_confidence=1.0,
            uncalibrated=is_provisional,
            degraded_subsystems=[],
            snapshot_id=None,
            as_of=normalised_as_of,
            effective_as_of=resolve_effective_as_of(normalised_as_of),
            meta=meta,
            similar_past=similar_past,
        )
        result = response.model_dump(mode="json")
        # Inject calibrated flag — AP4 explicit contract field.
        result["calibrated"] = is_calibrated
        return result


# ---------------------------------------------------------------------------
# Scoring-config loading (requires DB session from app.state)
# ---------------------------------------------------------------------------


async def _load_scoring_config(
    app: FastAPI, workspace_id: uuid.UUID
) -> IncidentScoringConfig | None:
    """Look up the per-tenant scoring config; return ``None`` to use v0.1.

    Tolerates a missing or unwired ``db_session_factory`` so unit tests
    that only stub the graph store keep passing.
    """
    factory = getattr(app.state, "db_session_factory", None)
    if factory is None:
        return None
    try:
        async with factory() as session:
            return await load_workspace_config(session, workspace_id)
    except Exception:
        log.warning(
            "scoring_config_load_failed",
            workspace_id=str(workspace_id),
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Similar-incident augmentation (issue #233, additive only)
# ---------------------------------------------------------------------------


async def _augment_with_similar_past(
    *,
    app: FastAPI,
    alert_id: str,
    workspace_id: uuid.UUID,
    as_of: datetime | None,
) -> list[dict[str, Any]]:
    """Best-effort call to :func:`collect_similar_past`."""
    try:
        from omniscience_server.similar_incidents import collect_similar_past
    except ImportError:  # pragma: no cover - defensive
        return []
    return await collect_similar_past(
        app=app,
        incident_id=alert_id,
        workspace_id=workspace_id,
        as_of=as_of,
    )


__all__ = [
    "ALERT_NOT_FOUND_CODE",
    "CONFIDENCE_ALERT_ONLY",
    "CONFIDENCE_NONE",
    "CONFIDENCE_PR_NO_TEMPORAL_MATCH",
    "CONFIDENCE_PR_TEMPORAL_MATCH",
    "CONFIDENCE_RESOURCE_ONLY",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "DEFAULT_MAX_DEPTH",
    "INVALID_ALERT_ID_CODE",
    "INVALID_ALERT_ID_MESSAGE",
    "MAX_MAX_DEPTH",
    "MAX_SLACK_THREADS_RETURNED",
    "MIN_MAX_DEPTH",
    "PR_RECENCY_WINDOW_SECONDS",
    "AlertIdRequest",
    "AlertSummary",
    "IncidentScoringConfig",
    "PrSummary",
    "ResolveIncidentResponse",
    "ResourceSummary",
    "SlackThreadSummary",
    "mcp_resolve_incident",
    "validate_alert_id",
]
