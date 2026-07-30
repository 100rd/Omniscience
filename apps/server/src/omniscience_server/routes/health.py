"""Health and readiness check endpoints.

Returns a structured JSON payload describing the liveness of each
infrastructure dependency.

Wave 2 status:
  - Postgres connection: real ``SELECT 1`` ping via ``app.state.db_engine``
    when the engine handle is present (task-sp-95-management-readonly-local-runtime).
  - NATS connection: real ping via NatsConnection.is_connected (issue #3)

task-sp-95-management-readonly-local-runtime (ADR-0023, OML-3): ``/ready`` now
covers the complete selected local dependency closure -- PostgreSQL, NATS,
Neo4j, Qdrant -- plus schema-migration and PW0/contract policy-init closure.
Each failing dependency downgrades ``/ready`` to unready and surfaces a *typed,
non-identifying* reason code (no host, DSN, credential, or payload content). A
process that is live while any load-bearing store is unavailable is unready --
never cached-current healthy (OML-3, AC-SP95-2).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import structlog
from fastapi import APIRouter, Request
from omniscience_core.queue import NatsConnection
from starlette.responses import JSONResponse

log = structlog.get_logger(__name__)

CheckStatus = Literal["healthy", "degraded", "unhealthy", "unchecked"]

router = APIRouter(tags=["ops"])

#: apps/server/src/omniscience_server/routes/health.py -> repo root (five
#: parents up: routes -> omniscience_server -> src -> server -> apps -> root).
#: The runtime image mirrors this same layout under /app (Dockerfile copies
#: contracts/ alongside apps/ and packages/), so the same relative walk
#: resolves correctly in both a local checkout and the built container.
_REPO_ROOT = Path(__file__).resolve().parents[5]

#: management-readonly-v1 release profile id (task-sp-86-management-readonly-release,
#: ADR-0022). Reported by /ready for operator/dashboard identification only --
#: never a management verdict, action recommendation, or authorization.
MANAGEMENT_READONLY_PROFILE_ID = "management-readonly-v1"

#: Typed, non-identifying readiness reason codes (task-sp-95, AC-SP95-2 /
#: ADR-0023 OML-3). A reason names *which* dependency/closure is not ready
#: without leaking a host, DSN, credential, tenant, workspace, or payload.
#: These are ops vocabulary only -- never a management verdict, recommendation,
#: or authorization (ADR-0022 read-only boundary).
_READINESS_REASONS: dict[str, str] = {
    "postgres": "postgres_unavailable",
    "nats": "nats_unavailable",
    "neo4j": "neo4j_unavailable",
    "qdrant": "qdrant_unavailable",
    "migration": "migration_incomplete",
    "mcp_contract": "mcp_contract_unavailable",
    "management_context_contract": "management_context_contract_unavailable",
    "pw0_privacy_contract": "pw0_privacy_contract_unavailable",
}


async def _check_postgres(request: Request) -> CheckStatus:
    """Verify PostgreSQL connectivity with a real ``SELECT 1`` round-trip.

    ``create_async_engine`` builds a lazy pool and does not open a socket at
    boot, so a running process can outlive a dropped database; the readiness
    probe therefore executes an actual query. Returns ``"unchecked"`` when no
    engine handle is present (e.g. an ASGI test client that never ran the
    lifespan), so unit tests without a live database stay green rather than
    reporting a false failure.
    """
    engine = getattr(request.app.state, "db_engine", None)
    if engine is None:
        return "unchecked"
    try:
        from sqlalchemy import text

        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:  # any driver/network error means "unavailable"
        return "unhealthy"
    return "healthy"


async def _check_migration(request: Request, postgres_status: CheckStatus) -> CheckStatus:
    """Verify the schema-migration closure is applied (Alembic ``alembic_version``).

    Only meaningful once PostgreSQL itself is reachable: when the database is
    down the ``postgres`` check already carries the reason, so migration is
    reported ``"unchecked"`` to avoid a misleading duplicate cause. A reachable
    database whose ``alembic_version`` table is absent or empty is
    ``"unhealthy"`` -- migrations have not completed (AC-SP95-2 migration
    closure), never silently healthy.
    """
    engine = getattr(request.app.state, "db_engine", None)
    if engine is None or postgres_status != "healthy":
        return "unchecked"
    try:
        from sqlalchemy import text

        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT version_num FROM alembic_version"))
            row = result.first()
    except Exception:  # table missing / not migrated
        return "unhealthy"
    return "healthy" if row is not None else "unhealthy"


async def _check_nats(nats_conn: NatsConnection | None) -> CheckStatus:
    """Verify NATS JetStream connectivity via the live connection object.

    Returns:
        ``"healthy"`` when the connection is open and confirmed connected.
        ``"unhealthy"`` when a connection object exists but is disconnected.
        ``"unchecked"`` when the connection has not been initialised yet
        (e.g. during testing without a real NATS server).
    """
    if nats_conn is None:
        return "unchecked"
    return "healthy" if nats_conn.is_connected else "unhealthy"


async def _check_neo4j(request: Request) -> CheckStatus:
    """Verify Neo4j graph-store connectivity via the live driver.

    Probes the same driver the application uses (``app.state.neo4j_graph_store``)
    with the neo4j driver's own ``verify_connectivity()``. Returns
    ``"unchecked"`` when no store handle is present (lifespan not run), so unit
    tests stay green; ``"unhealthy"`` on any connectivity error (AC-SP95-2).
    """
    store = getattr(request.app.state, "neo4j_graph_store", None)
    driver = getattr(store, "_driver", None)
    if driver is None:
        return "unchecked"
    try:
        await driver.verify_connectivity()
    except Exception:  # any connectivity error means "unavailable"
        return "unhealthy"
    return "healthy"


async def _check_qdrant(request: Request) -> CheckStatus:
    """Verify Qdrant vector-store connectivity via the live client.

    Probes ``app.state.qdrant_store`` with a lightweight ``get_collections()``
    listing. Returns ``"unchecked"`` when no store/client handle is present
    (lifespan not run); ``"unhealthy"`` on any connectivity error (AC-SP95-2).
    """
    store = getattr(request.app.state, "qdrant_store", None)
    client = getattr(store, "_client", None)
    if client is None:
        return "unchecked"
    try:
        await client.get_collections()
    except Exception:  # any connectivity error means "unavailable"
        return "unhealthy"
    return "healthy"


def _aggregate_status(checks: dict[str, CheckStatus]) -> CheckStatus:
    """Derive overall status from individual dependency checks."""
    statuses = set(checks.values())
    if "unhealthy" in statuses:
        return "unhealthy"
    if "degraded" in statuses:
        return "degraded"
    return "healthy"


def _reasons_for(checks: dict[str, CheckStatus]) -> dict[str, str]:
    """Map every non-healthy, non-``unchecked`` check to its typed reason code.

    ``unchecked`` is a "not applicable here" state (no live handle), not a
    failure, so it carries no reason -- keeping the readiness body free of
    spurious reasons when the app is exercised without live dependencies.
    """
    return {
        name: _READINESS_REASONS[name]
        for name, status in checks.items()
        if status in ("unhealthy", "degraded") and name in _READINESS_REASONS
    }


def _check_contract_pin(name: str, required_key: str) -> CheckStatus:
    """Lightweight presence + structural check for ``contracts/<name>/pin.json``.

    This is NOT a full digest re-verification -- recomputing every schema's
    sha256 on every readiness poll would make /ready an expensive hot path.
    Full digest re-derivation is ``scripts/qualify_management_readonly_release.py``'s
    job (AC-SP86-1); this check only proves the release's contract closure is
    present and structurally intact in the running container, so a missing or
    corrupted contracts/ directory becomes visibly unhealthy rather than a
    silent gap (AC-SP86-5 "observable ... without Omnius or a model/provider").
    """
    pin_path = _REPO_ROOT / "contracts" / name / "pin.json"
    try:
        pin = json.loads(pin_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unhealthy"
    if not isinstance(pin, dict) or not pin.get(required_key):
        return "unhealthy"
    return "healthy"


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    """Return the health of the service and its dependencies.

    Returns HTTP 503 when status is unhealthy, 200 otherwise.
    """
    settings = request.app.state.settings
    nats_conn: NatsConnection | None = getattr(request.app.state, "nats", None)

    checks: dict[str, CheckStatus] = {
        "postgres": await _check_postgres(request),
        "nats": await _check_nats(nats_conn),
    }
    overall = _aggregate_status(checks)
    log.info("health_check", status=overall, checks=checks)

    status_code = 503 if overall == "unhealthy" else 200
    return JSONResponse(
        content={
            "status": overall,
            "checks": checks,
            "version": settings.app_version,
        },
        status_code=status_code,
    )


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Readiness for the selected management-readonly-v1 read surfaces
    (task-sp-86-management-readonly-release AC-SP86-5;
    task-sp-95-management-readonly-local-runtime AC-SP95-2, ADR-0023 OML-3).

    Covers the complete selected local dependency closure -- PostgreSQL, NATS,
    Neo4j, Qdrant -- plus schema-migration closure and the release's own PW0 /
    management-context / MCP contract policy-init closure. Any unavailable
    load-bearing dependency downgrades readiness and surfaces a typed,
    non-identifying reason code in ``reasons`` (no host/DSN/credential/tenant/
    payload). This reports dependency health only -- it is ops evidence, never
    a management verdict, action recommendation, authorization, or privacy
    verdict (ADR-0022's read-only boundary). Returns HTTP 503 when any check is
    unhealthy, 200 otherwise -- same convention as /health so existing
    readiness-probe wiring applies unchanged.
    """
    settings = request.app.state.settings
    nats_conn: NatsConnection | None = getattr(request.app.state, "nats", None)

    postgres_status = await _check_postgres(request)
    checks: dict[str, CheckStatus] = {
        "postgres": postgres_status,
        "nats": await _check_nats(nats_conn),
        "neo4j": await _check_neo4j(request),
        "qdrant": await _check_qdrant(request),
        "migration": await _check_migration(request, postgres_status),
        "mcp_contract": _check_contract_pin("mcp", "vendoredEntries"),
        "management_context_contract": _check_contract_pin("management", "ownedEntries"),
        "pw0_privacy_contract": _check_contract_pin("pii", "vendoredEntries"),
    }
    overall = _aggregate_status(checks)
    reasons = _reasons_for(checks)
    log.info(
        "readiness_check",
        status=overall,
        checks=checks,
        reasons=reasons,
        profile_id=MANAGEMENT_READONLY_PROFILE_ID,
    )

    status_code = 503 if overall == "unhealthy" else 200
    return JSONResponse(
        content={
            "status": overall,
            "profile_id": MANAGEMENT_READONLY_PROFILE_ID,
            "checks": checks,
            "reasons": reasons,
            "version": settings.app_version,
        },
        status_code=status_code,
    )
