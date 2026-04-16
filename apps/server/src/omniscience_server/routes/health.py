"""Health check endpoint.

Returns a structured JSON payload describing the liveness of each
infrastructure dependency.  Placeholder check functions return ``healthy``
until real connections are wired in Wave 2.

Wave 2 references:
  - Postgres connection: issue #2 (database layer)
  - NATS connection: issue #3 (NATS JetStream integration)
"""

from __future__ import annotations

from typing import Literal

import structlog
from fastapi import APIRouter

log = structlog.get_logger(__name__)

CheckStatus = Literal["healthy", "degraded", "unhealthy", "unchecked"]

router = APIRouter(tags=["ops"])


async def _check_postgres() -> CheckStatus:
    """Verify PostgreSQL connectivity.

    TODO(wave-2, issue-#2): Replace with a real asyncpg/SQLAlchemy ping.
    """
    return "unchecked"


async def _check_nats() -> CheckStatus:
    """Verify NATS JetStream connectivity.

    TODO(wave-2, issue-#3): Replace with a real nats-py connection check.
    """
    return "unchecked"


def _aggregate_status(checks: dict[str, CheckStatus]) -> CheckStatus:
    """Derive overall status from individual dependency checks."""
    statuses = set(checks.values())
    if "unhealthy" in statuses:
        return "unhealthy"
    if "degraded" in statuses:
        return "degraded"
    return "healthy"


@router.get("/health")
async def health() -> dict[str, object]:
    """Return the health of the service and its dependencies.

    Response shape::

        {
            "status": "healthy" | "degraded" | "unhealthy",
            "checks": {
                "postgres": "healthy" | "degraded" | "unhealthy" | "unchecked",
                "nats":     "healthy" | "degraded" | "unhealthy" | "unchecked"
            },
            "version": "0.1.0"
        }
    """
    checks: dict[str, CheckStatus] = {
        "postgres": await _check_postgres(),
        "nats": await _check_nats(),
    }
    overall = _aggregate_status(checks)
    log.info("health_check", status=overall, checks=checks)
    return {
        "status": overall,
        "checks": checks,
        "version": "0.1.0",
    }
