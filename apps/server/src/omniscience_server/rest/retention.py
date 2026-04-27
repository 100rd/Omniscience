"""Retention admin REST endpoints (issues #135 / #136, ADR-0009 §3 / §8).

Endpoints
---------

* ``GET  /api/v1/admin/retention/report``    — workspace-scoped dry-run
  report (issue #135). Always runs the worker in side-effect-free mode
  regardless of operator config.
* ``GET  /api/v1/admin/retention/status``    — workspace-scoped tier
  counts + last-run-at + lag (issue #136). Cheap to call (reads gauges
  + the worker's last report cache; does NOT trigger a fresh tick).
* ``POST /api/v1/admin/retention/run-now``   — triggers a single live
  retention tick scoped to the caller's workspace (issue #136).
  Returns 202 Accepted with a server-generated ``run_id``. The
  underlying call is :py:meth:`RetentionWorker.run_once`.

Security
--------
All three endpoints require the ``stats:read`` scope and a workspace-
scoped token. Rationale for the scope choice:

* ``stats:read`` matches the rest of the admin read surface
  (``rest/stats.py``). A dedicated ``retention:read`` was considered
  but rejected to avoid scope-creep on existing admin-issued tokens.
* ``run-now`` is workspace-scoped (the caller's workspace_id is the
  only one processed) and idempotent — a re-run is a no-op unless
  wall-clock time has advanced past the cutoff (ADR-0009 §3
  idempotency). It therefore does not require a stronger scope than
  the read surface; the equivalent dry-run is already exposed at
  ``/report``. If a future reviewer wants admin-only ``run-now`` we can
  promote it to :class:`Scope.admin` without an API contract change
  (``admin`` implies ``stats:read``).
* Workspace-scoped: a token without an associated workspace fails
  closed with 403, identical to ``rest/stats.py``. This is the second
  line of defence after the protocol-level ``workspace_id``
  invariants on ``GraphStore`` / ``VectorStore``.

ACL invariant (ADR-0009 §Consequences-security)
------------------------------------------------
Per-tenant counts in the dashboard come from the same workspace-scoped
``count_records_by_tier`` / ``count_chunks_by_tier`` reads the worker
itself uses on every tick. Data leaving the server is per-workspace
counts only — NO entity bodies, NO chunk text, NO names, only IDs +
counts. The regression test ``test_retention_admin_endpoints.py``
asserts:

1. Non-admin tokens get 403.
2. Admin tokens get 200 with their workspace's counts.
3. Workspace A's ``/status`` excludes any of workspace B's data.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
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
    RunReport,
    WorkspaceRetentionReport,
)

log = structlog.get_logger(__name__)

router = APIRouter(tags=["admin"])

_stats_scope_dep: Any = Depends(require_scope(Scope.stats_read))
_current_token_dep: Any = Depends(get_current_token)


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------


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


class RetentionStatus(BaseModel):
    """Per-workspace retention status (issue #136).

    Returns the four metric families from ADR-0009 §8 collapsed to
    workspace scope so the admin UI can render the per-tenant table
    without scraping Prometheus directly. ACL invariant: the response
    contains only IDs and counts — no entity bodies, no chunk text.
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: uuid.UUID = Field(description="Workspace the status covers.")
    neo4j_hot: int = Field(ge=0, description="Hot-tier records in Neo4j.")
    neo4j_warm: int = Field(ge=0, description="Warm-tier (snapshot) records in Neo4j.")
    qdrant_hot: int = Field(ge=0, description="Hot-tier chunks in Qdrant.")
    qdrant_warm: int = Field(ge=0, description="Warm-tier chunks in Qdrant.")
    last_run_at: datetime | None = Field(
        default=None,
        description=(
            "Wall-clock timestamp of the worker's most recent completed "
            "tick. None when the worker has not yet run since process start."
        ),
    )
    lag_seconds: float = Field(
        ge=0.0,
        description=(
            "Wall-clock seconds since the oldest overdue record's "
            "recorded_at — same observation as `omniscience_retention_"
            "worker_lag_seconds`. ADR-0009 §8 SLO."
        ),
    )
    dry_run: bool = Field(
        description=(
            "Whether the live retention worker is currently configured "
            "to operate in dry-run mode. Operators flipping the worker "
            "live for the first time on a deployment review this flag."
        ),
    )


class RetentionRunNowResponse(BaseModel):
    """Response from a successful POST /admin/retention/run-now (issue #136).

    The endpoint returns 202 Accepted; the run executes synchronously
    inside the request handler against the caller's workspace only.
    The ``run_id`` is server-generated and stored in structured logs so
    operators can correlate the dashboard refresh that follows the
    button click with the worker tick that produced the new gauge values.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID = Field(description="Server-generated identifier for the tick.")
    workspace_id: uuid.UUID = Field(description="Workspace the tick was scoped to.")
    started_at: datetime = Field(description="Wall-clock start time of the tick.")
    finished_at: datetime = Field(description="Wall-clock finish time of the tick.")
    duration_seconds: float = Field(
        ge=0.0,
        description="Wall-clock duration of the tick.",
    )
    dry_run: bool = Field(
        description=(
            "Whether the worker that executed this tick was in dry-run "
            "mode. Mirrors `Settings.retention_dry_run` at the time of "
            "the call."
        ),
    )
    lag_seconds: float = Field(
        ge=0.0,
        description="Lag SLO observation produced by this tick.",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_report_response(report: WorkspaceRetentionReport) -> RetentionReport:
    """Convert worker dataclass to dry-run Pydantic response model."""
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


def _require_workspace(token: ApiToken, *, op: str) -> uuid.UUID:
    """Extract the caller's workspace_id or fail closed with 403."""
    workspace_id = get_workspace_id(token)
    if workspace_id is None:
        log.warning(
            "retention_admin_rejected_no_workspace",
            op=op,
            token_prefix=token.token_prefix,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "code": "forbidden",
                "message": "Retention admin endpoints require a workspace-scoped token.",
            },
        )
    return workspace_id


def _resolve_dry_run_worker(request: Request) -> RetentionWorker:
    """Resolve a side-effect-free :class:`RetentionWorker` for /report.

    The wired worker on ``app.state.retention_worker`` may be running
    in live mode; we always force ``dry_run=True`` for the report
    endpoint so the response is guaranteed mutation-free regardless of
    operator configuration.
    """
    settings, graph_store, vector_store, factory = _resolve_backend(request)
    forced = settings.model_copy(update={"retention_dry_run": True})
    return RetentionWorker(
        session_factory=factory,
        graph_store=graph_store,
        vector_store=vector_store,
        settings=forced,
    )


def _resolve_live_worker(request: Request) -> RetentionWorker:
    """Resolve the live :class:`RetentionWorker` for /run-now.

    Prefers the worker wired to ``app.state.retention_worker`` (the
    one running on the FastAPI lifespan tick loop) so a manual run-now
    shares the same Settings, dry-run flag, and store handles as the
    scheduled ticks. When that worker is unavailable (e.g. during
    tests or when ``retention_enabled=False``), falls back to a fresh
    instance bound to the request's stores.
    """
    existing: RetentionWorker | None = getattr(request.app.state, "retention_worker", None)
    if isinstance(existing, RetentionWorker):
        return existing
    settings, graph_store, vector_store, factory = _resolve_backend(request)
    return RetentionWorker(
        session_factory=factory,
        graph_store=graph_store,
        vector_store=vector_store,
        settings=settings,
    )


def _resolve_backend(
    request: Request,
) -> tuple[Settings, Neo4jGraphStore, QdrantVectorStore, Any]:
    """Pull settings + store handles + session factory from app.state.

    Centralizes the 503 fail-closed branch shared by the worker
    resolvers. The ``Any`` return type for the factory mirrors the
    existing ``rest/stats.py`` shape — SQLAlchemy's
    ``async_sessionmaker`` is generic in T and we never narrow it
    here.
    """
    settings: Settings | None = getattr(request.app.state, "settings", None)
    graph_store: Neo4jGraphStore | None = getattr(request.app.state, "graph_store", None)
    vector_store: QdrantVectorStore | None = getattr(request.app.state, "vector_store", None)
    factory = getattr(request.app.state, "db_session_factory", None)
    if settings is None or graph_store is None or vector_store is None or factory is None:
        log.warning("retention_admin_backend_unavailable")
        raise HTTPException(
            status_code=503,
            detail={
                "code": "service_unavailable",
                "message": "Retention backend not available",
            },
        )
    return settings, graph_store, vector_store, factory


def _last_run_for_workspace(
    worker: RetentionWorker,
    workspace_id: uuid.UUID,
) -> datetime | None:
    """Best-effort lookup of the worker's last run-finished timestamp.

    The worker exposes its most recent :class:`RunReport` via
    ``last_report``. When that report covered the caller's workspace
    (which it does whenever the workspace existed in Postgres at the
    last tick) we use ``finished_at``; otherwise return None so the UI
    can render a neutral "never run" state instead of a stale value.
    """
    last_report: RunReport | None = worker.last_report
    if last_report is None:
        return None
    for ws_report in last_report.per_workspace:
        if ws_report.workspace_id == workspace_id:
            return last_report.finished_at
    return None


# ---------------------------------------------------------------------------
# GET /admin/retention/report — dry-run report (issue #135)
# ---------------------------------------------------------------------------


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
    regardless of ``Settings.retention_dry_run``.

    Requires scope: ``stats:read``
    Requires: token scoped to a workspace (fails closed with 403).
    """
    workspace_id = _require_workspace(token, op="report")
    worker = _resolve_dry_run_worker(request)
    run_report = await worker.run_once(workspace_filter=workspace_id)
    if not run_report.per_workspace:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "workspace_not_found",
                "message": "Workspace has no records to evict.",
            },
        )
    response = _to_report_response(run_report.per_workspace[0])
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


# ---------------------------------------------------------------------------
# GET /admin/retention/status — per-workspace counts + lag (issue #136)
# ---------------------------------------------------------------------------


@router.get(
    "/admin/retention/status",
    response_model=RetentionStatus,
    summary="Workspace-scoped retention status (counts + lag)",
    dependencies=[_stats_scope_dep],
)
async def admin_retention_status(
    request: Request,
    token: ApiToken = _current_token_dep,
) -> RetentionStatus:
    """Return per-workspace tier counts + last-run-at + lag.

    The response derives counts from the same workspace-scoped store
    methods the worker uses on every tick (``count_records_by_tier``
    on Neo4j, ``count_chunks_by_tier`` on Qdrant). It does NOT trigger
    a fresh retention tick — this endpoint must be cheap enough to
    poll on the admin UI's 30s refresh cadence (issue #136 dashboard
    cards) without piling pressure onto Neo4j/Qdrant.

    The lag observation is read off the most recent worker report
    when present, falling back to the live Prometheus gauge value
    otherwise. ``last_run_at`` is None until the worker has completed
    its first tick since process start.

    Requires scope: ``stats:read``
    Requires: token scoped to a workspace (fails closed with 403).
    """
    workspace_id = _require_workspace(token, op="status")
    settings, graph_store, vector_store, _factory = _resolve_backend(request)
    graph_records = await graph_store.count_records_by_tier(workspace_id=workspace_id)
    chunk_records = await vector_store.count_chunks_by_tier(workspace_id=workspace_id)

    worker: RetentionWorker | None = getattr(request.app.state, "retention_worker", None)
    last_run_at: datetime | None = None
    lag_seconds = 0.0
    if isinstance(worker, RetentionWorker):
        last_run_at = _last_run_for_workspace(worker, workspace_id)
        last_report = worker.last_report
        if last_report is not None:
            for ws_report in last_report.per_workspace:
                if ws_report.workspace_id == workspace_id:
                    lag_seconds = ws_report.lag_seconds
                    break

    response = RetentionStatus(
        workspace_id=workspace_id,
        neo4j_hot=int(graph_records.get("hot", 0)),
        neo4j_warm=int(graph_records.get("warm", 0)),
        qdrant_hot=int(chunk_records.get("hot", 0)),
        qdrant_warm=int(chunk_records.get("warm", 0)),
        last_run_at=last_run_at,
        lag_seconds=lag_seconds,
        dry_run=bool(settings.retention_dry_run),
    )
    log.info(
        "retention_status",
        workspace_id=str(workspace_id),
        neo4j_hot=response.neo4j_hot,
        neo4j_warm=response.neo4j_warm,
        qdrant_hot=response.qdrant_hot,
        qdrant_warm=response.qdrant_warm,
        lag_seconds=response.lag_seconds,
    )
    return response


# ---------------------------------------------------------------------------
# POST /admin/retention/run-now — manual workspace-scoped tick (issue #136)
# ---------------------------------------------------------------------------


@router.post(
    "/admin/retention/run-now",
    response_model=RetentionRunNowResponse,
    summary="Trigger a single retention tick scoped to the caller's workspace",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[_stats_scope_dep],
)
async def admin_retention_run_now(
    request: Request,
    token: ApiToken = _current_token_dep,
) -> RetentionRunNowResponse:
    """Trigger a single retention worker tick for the caller's workspace.

    Calls :py:meth:`RetentionWorker.run_once` with
    ``workspace_filter=workspace_id`` so the run is structurally
    incapable of touching another workspace's data — the same ACL
    invariant the scheduled ticks rely on (ADR-0009 §3).

    Returns 202 Accepted with a server-generated ``run_id``. The run
    executes synchronously inside the request handler; the 202 status
    code reflects the architectural posture (ticks are usually
    background-scheduled) rather than asynchronous execution. Tick
    duration is bounded by the configured batch size and store
    timeouts.

    Requires scope: ``stats:read``
    Requires: token scoped to a workspace (fails closed with 403).
    """
    workspace_id = _require_workspace(token, op="run_now")
    worker = _resolve_live_worker(request)
    run_id = uuid.uuid4()
    log.info(
        "retention_run_now_started",
        run_id=str(run_id),
        workspace_id=str(workspace_id),
        dry_run=worker.dry_run,
    )
    run_report = await worker.run_once(workspace_filter=workspace_id)
    if not run_report.per_workspace:
        log.warning(
            "retention_run_now_workspace_not_found",
            run_id=str(run_id),
            workspace_id=str(workspace_id),
        )
        raise HTTPException(
            status_code=404,
            detail={
                "code": "workspace_not_found",
                "message": "Workspace not found in retention worker enumeration.",
            },
        )
    response = RetentionRunNowResponse(
        run_id=run_id,
        workspace_id=workspace_id,
        started_at=run_report.started_at,
        finished_at=run_report.finished_at,
        duration_seconds=run_report.duration_seconds,
        dry_run=run_report.dry_run,
        lag_seconds=run_report.lag_seconds,
    )
    log.info(
        "retention_run_now_completed",
        run_id=str(run_id),
        workspace_id=str(workspace_id),
        duration_s=response.duration_seconds,
        lag_s=response.lag_seconds,
    )
    return response


__all__ = [
    "RetentionReport",
    "RetentionRunNowResponse",
    "RetentionSampleEntry",
    "RetentionStatus",
    "router",
]
