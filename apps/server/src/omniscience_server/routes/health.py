"""Health check endpoint.

Returns a structured JSON payload describing the liveness of each
infrastructure dependency.

Wave 2 status:
  - Postgres connection: placeholder (issue #2, database layer)
  - NATS connection: real ping via NatsConnection.is_connected (issue #3)
"""

from __future__ import annotations

from typing import Literal

import structlog
from fastapi import APIRouter, Request
from omniscience_core.queue import NatsConnection
from starlette.responses import JSONResponse

log = structlog.get_logger(__name__)

CheckStatus = Literal["healthy", "degraded", "unhealthy", "unchecked"]

router = APIRouter(tags=["ops"])


async def _check_postgres() -> CheckStatus:
    """Verify PostgreSQL connectivity.

    TODO(wave-2, issue-#2): Replace with a real asyncpg/SQLAlchemy ping.
    """
    return "unchecked"


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


def _aggregate_status(checks: dict[str, CheckStatus]) -> CheckStatus:
    """Derive overall status from individual dependency checks."""
    statuses = set(checks.values())
    if "unhealthy" in statuses:
        return "unhealthy"
    if "degraded" in statuses:
        return "degraded"
    return "healthy"


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    """Return the health of the service and its dependencies.

    Returns HTTP 503 when status is unhealthy, 200 otherwise.
    """
    settings = request.app.state.settings
    nats_conn: NatsConnection | None = getattr(request.app.state, "nats", None)

    checks: dict[str, CheckStatus] = {
        "postgres": await _check_postgres(),
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
