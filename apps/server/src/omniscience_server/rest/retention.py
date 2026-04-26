"""Retention dry-run report REST endpoint (issue #135, ADR-0009 §3).

Exposes ``GET /api/v1/admin/retention/report`` — workspace-scoped,
returns the dry-run report for the caller's workspace.

Security
--------
- Requires the ``stats:read`` scope (matches the rest of the admin
  read surface; a dedicated ``retention:read`` was considered but
  rejected to avoid scope-creep on existing tokens).
- Workspace-scoped: a token without an associated workspace fails
  closed with 403, identical to ``rest/stats.py``.  This is the second
  line of defence after the protocol-level ``workspace_id`` invariants
  on ``GraphStore`` / ``VectorStore``.
- The endpoint NEVER mutates either store — it always runs the
  retention pipeline in dry-run mode regardless of the worker's
  current ``retention_dry_run`` setting.  Operators reviewing
  eviction posture before flipping the worker live MUST be able to
  call this without side effects.

Response shape
--------------
:class:`RetentionReport` (Pydantic model below).  Sample is bounded by
``Settings.retention_sample_size`` (default 20).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from omniscience_core.auth.middleware import get_current_token, require_scope
from omniscience_core.auth.scopes import Scope
from omniscience_core.auth.workspace import get_workspace_id
from omniscience_core.config import Settings
from omniscience_core.db.models import ApiToken
from omniscience_index.stores.neo4j_store import Neo4jGraphStore
from omniscience_index.stores.qdrant_store import QdrantVectorStore
from pydantic import BaseModel, ConfigDict, Field

from omniscience_server.retention_worker import (
    RetentionWorker,
    WorkspaceRetentionReport,
)

log = structlog.get_logger(__name__)

router = APIRouter(tags=["admin"])

_stats_scope_dep: Any = Depends(require_scope(Scope.stats_read))
_current_token_dep: Any = Depends(get_current_token)


class RetentionSampleEntry(BaseModel):
    """One sampled :EntityState row from the eligible cohort."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    valid_from: str | None = None
    recorded_at: str | None = None


class RetentionReport(BaseModel):
    """Per-workspace dry-run report (ADR-0009 §3).

    Counts and a sampled record set are returned; no record contents
    are mutated.  The report carries the lag SLO observation (ADR-0009
    §8) so operators can see, in the same response, both *what* would
    be evicted and *how overdue* the worker currently is.
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: uuid.UUID = Field(description="Workspace the report covers.")
    dry_run: bool = Field(
        description=(
            "Always True for this endpoint — the response is generated "
            "by a side-effect-free pipeline run regardless of the "
            "worker's live `retention_dry_run` configuration."
        ),
    )
    eligible_hot_to_warm_entity_states: int = Field(
        ge=0,
        description=":EntityState rows with recorded_at < hot cutoff.",
    )
    eligible_hot_to_warm_edges: int = Field(
        ge=0,
        description="End-dated relationships with recorded_at < hot cutoff.",
    )
    eligible_hot_to_warm_chunks: int = Field(
        ge=0,
        description="Qdrant chunks with recorded_at < hot cutoff (ADR-0009 §5).",
    )
    eligible_warm_to_archive_entity_snapshots: int = Field(
        ge=0,
        description=":EntitySnapshot:Daily rows older than warm cutoff.",
    )
    eligible_warm_to_archive_dates: list[date] = Field(
        default_factory=list,
        description=(
            "Distinct snapshot dates that would be archived this run "
            "(bounded by RETENTION_ARCHIVE_DATES_PER_RUN)."
        ),
    )
    sampled_eligible: list[RetentionSampleEntry] = Field(
        default_factory=list,
        description="Up to retention_sample_size sampled hot-to-warm rows.",
    )
    oldest_eligible_recorded_at: datetime | None = Field(
        default=None,
        description=(
            "Oldest recorded_at currently overdue for eviction. "
            "When None the workspace has no overdue rows."
        ),
    )
    lag_seconds: float = Field(
        ge=0.0,
        description=(
            "Wall-clock seconds since the oldest overdue row's "
            "recorded_at; surfaces the deployment-wide ADR-0009 §8 "
            "SLO (<24h steady, P1 at >7d)."
        ),
    )


def _to_response(report: WorkspaceRetentionReport) -> RetentionReport:
    """Convert worker dataclass to Pydantic response model."""
    return RetentionReport(
        workspace_id=report.workspace_id,
        dry_run=True,
        eligible_hot_to_warm_entity_states=report.eligible_hot_to_warm_entity_states,
        eligible_hot_to_warm_edges=report.eligible_hot_to_warm_edges,
        eligible_hot_to_warm_chunks=report.eligible_hot_to_warm_chunks,
        eligible_warm_to_archive_entity_snapshots=(
            report.eligible_warm_to_archive_entity_snapshots
        ),
        eligible_warm_to_archive_dates=list(report.eligible_warm_to_archive_dates),
        sampled_eligible=[
            RetentionSampleEntry(
                id=entry.get("id"),
                valid_from=entry.get("valid_from"),
                recorded_at=entry.get("recorded_at"),
            )
            for entry in report.sampled_eligible
        ],
        oldest_eligible_recorded_at=report.oldest_eligible_recorded_at,
        lag_seconds=report.lag_seconds,
    )


def _require_workspace(token: ApiToken) -> uuid.UUID:
    """Extract the caller's workspace_id or fail closed with 403."""
    workspace_id = get_workspace_id(token)
    if workspace_id is None:
        log.warning(
            "retention_report_rejected_no_workspace",
            token_prefix=token.token_prefix,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "code": "forbidden",
                "message": ("Retention report endpoint requires a workspace-scoped token."),
            },
        )
    return workspace_id


def _resolve_dry_run_worker(request: Request) -> RetentionWorker:
    """Resolve a side-effect-free :class:`RetentionWorker`.

    The wired worker on ``app.state.retention_worker`` may be running
    in live mode; we always force ``dry_run=True`` for the report
    endpoint so the response is guaranteed mutation-free regardless of
    operator configuration.
    """
    settings: Settings | None = getattr(request.app.state, "settings", None)
    graph_store: Neo4jGraphStore | None = getattr(request.app.state, "graph_store", None)
    vector_store: QdrantVectorStore | None = getattr(request.app.state, "vector_store", None)
    factory = getattr(request.app.state, "db_session_factory", None)
    if settings is None or graph_store is None or vector_store is None or factory is None:
        log.warning("retention_report_backend_unavailable")
        raise HTTPException(
            status_code=503,
            detail={
                "code": "service_unavailable",
                "message": "Retention backend not available",
            },
        )
    # Force dry-run regardless of the live worker's config.
    forced = settings.model_copy(update={"retention_dry_run": True})
    return RetentionWorker(
        session_factory=factory,
        graph_store=graph_store,
        vector_store=vector_store,
        settings=forced,
    )


@router.get(
    "/admin/retention/report",
    response_model=RetentionReport,
    summary="Workspace-scoped dry-run retention report",
    dependencies=[_stats_scope_dep],
)
async def admin_retention_report(
    request: Request,
    token: ApiToken = _current_token_dep,
) -> RetentionReport:
    """Return what eviction would do for the caller's workspace.

    Side-effect free: the call always runs the worker in dry-run mode
    regardless of ``Settings.retention_dry_run``.  The caller sees:

    * Counts of rows eligible for hot-to-warm and warm-to-archive
      transitions (Neo4j entity states, edges, Qdrant chunks).
    * The distinct snapshot dates that would be archived this run.
    * A sampled set of eligible :EntityState rows for the dry-run
      report (ADR-0009 §3 — "produces a structured 'would-evict'
      report").
    * Lag SLO observation in seconds (ADR-0009 §8).

    Requires scope: ``stats:read``
    Requires: token scoped to a workspace (fails closed with 403).
    """
    workspace_id = _require_workspace(token)
    worker = _resolve_dry_run_worker(request)
    run_report = await worker.run_once(workspace_filter=workspace_id)
    if not run_report.per_workspace:
        # Workspace exists in the token's scope but has no rows in
        # Postgres `workspaces` (e.g. a token claiming a tenant that
        # was offboarded).  Fail closed identically to stats endpoints.
        raise HTTPException(
            status_code=404,
            detail={
                "code": "workspace_not_found",
                "message": "Workspace has no records to evict.",
            },
        )
    response = _to_response(run_report.per_workspace[0])
    log.info(
        "retention_report",
        workspace_id=str(workspace_id),
        eligible_es=response.eligible_hot_to_warm_entity_states,
        eligible_edges=response.eligible_hot_to_warm_edges,
        eligible_chunks=response.eligible_hot_to_warm_chunks,
        eligible_archive=response.eligible_warm_to_archive_entity_snapshots,
        lag_seconds=response.lag_seconds,
    )
    return response


__all__ = ["RetentionReport", "RetentionSampleEntry", "router"]
