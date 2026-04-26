"""ClientsStatsService — connected-clients telemetry rollup (issue #113).

Why this is a separate service from :class:`StatsService`
---------------------------------------------------------
:class:`omniscience_core.stats.StatsService` aggregates *content* counts
(documents, chunks, entities, edges) across Postgres + Neo4j + Qdrant.
This service aggregates *operational* counts (per-token request volume,
per-session MCP activity) from a tiny in-process bucket structure plus
Postgres for the workspace-filtered token list.  The dependency surfaces
do not overlap, and mixing them would force every dashboard read to drag
the graph + vector clients into scope.

ACL invariant (the non-negotiable bit)
--------------------------------------
The endpoint at ``GET /api/v1/stats/clients`` must NEVER return tokens
that belong to a workspace other than the caller's.  This service is
the single place where the filter happens:

    1. Query Postgres for ``ApiToken`` rows whose
       :func:`workspace_filter` matches the caller's workspace.
    2. Pass the resulting *list of token UUIDs* to
       :meth:`ClientTelemetryRegistry.snapshot_tokens`.
    3. Assemble :class:`ClientsStatsResponse`.

The Prometheus counters do still carry ``token_id`` labels for tokens
across all workspaces — but the endpoint never reads those counters
directly.  External scrapers that have access to the raw metrics
endpoint are already trusted at the deployment level; the dashboard,
which is operator-facing, goes through this service.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Final

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from omniscience_core.auth.workspace import workspace_filter
from omniscience_core.db.models import ApiToken
from omniscience_core.stats.cache import TTLCache
from omniscience_core.stats.models import (
    ClientsStatsResponse,
    TokenClientStats,
    ToolUsageEntry,
)
from omniscience_core.telemetry.clients import (
    ClientTelemetryRegistry,
    get_client_registry,
)

log = structlog.get_logger(__name__)

#: Cache TTL for the clients-stats endpoint.  Issue #113 calls for a
#: 5-second cache; this is short enough that the dashboard feels live
#: while still absorbing a burst of dashboard panels reloading on the
#: same tick.
CLIENTS_STATS_CACHE_TTL_SECONDS: Final[float] = 5.0

#: Maximum number of "top tools" returned in the response.  10 is more
#: than enough for the registered MCP tool count today (5).
_TOP_TOOLS_LIMIT: Final[int] = 10

#: Cache key constant — keeps refactors flowing through one place.
_METHOD_CLIENTS: Final[str] = "clients"


class ClientsStatsService:
    """Workspace-scoped roll-up of connected-clients telemetry."""

    def __init__(
        self,
        *,
        db_session_factory: async_sessionmaker[AsyncSession],
        registry: ClientTelemetryRegistry | None = None,
        cache: TTLCache[ClientsStatsResponse] | None = None,
    ) -> None:
        self._session_factory = db_session_factory
        self._registry = registry or get_client_registry()
        self._cache: TTLCache[ClientsStatsResponse] = cache or TTLCache(
            ttl_seconds=CLIENTS_STATS_CACHE_TTL_SECONDS
        )

    async def clients(self, *, workspace_id: uuid.UUID) -> ClientsStatsResponse:
        """Return the connected-clients rollup for ``workspace_id``."""

        async def _build() -> ClientsStatsResponse:
            return await self._build_clients(workspace_id=workspace_id)

        return await self._cache.get_or_compute(
            workspace_id=workspace_id,
            method=_METHOD_CLIENTS,
            factory=_build,
        )

    async def _build_clients(self, *, workspace_id: uuid.UUID) -> ClientsStatsResponse:
        """Assemble the response by joining the token list with telemetry."""
        tokens = await self._fetch_workspace_tokens(workspace_id=workspace_id)
        token_id_strs = [str(t.id) for t in tokens]
        token_name_by_id = {str(t.id): t.name for t in tokens}

        snapshots = self._registry.snapshot_tokens(token_ids=token_id_strs)
        sessions = self._registry.snapshot_sessions()
        top_tools = self._registry.snapshot_top_tools(limit=_TOP_TOOLS_LIMIT)

        token_rows = [
            TokenClientStats(
                token_id=snap.token_id,
                name=token_name_by_id.get(str(snap.token_id), "<unknown>"),
                last_seen_at=_unix_to_dt(snap.last_seen_at_unix),
                requests_last_15m=snap.requests_last_15m,
                requests_last_24h=snap.requests_last_24h,
            )
            for snap in snapshots
        ]
        return ClientsStatsResponse(
            mcp_sessions_active=sessions.active,
            mcp_sessions_last_hour=sessions.last_hour,
            tokens=token_rows,
            top_tools_last_hour=[
                ToolUsageEntry(
                    tool_name=t.tool_name,
                    invocations_last_hour=t.invocations_last_hour,
                )
                for t in top_tools
            ],
        )

    async def _fetch_workspace_tokens(self, *, workspace_id: uuid.UUID) -> list[ApiToken]:
        """Return the ApiToken rows visible to ``workspace_id``."""
        stmt = workspace_filter(
            select(ApiToken).where(ApiToken.is_active.is_(True)),
            workspace_id,
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return list(result.scalars().all())


def _unix_to_dt(value: float | None) -> datetime | None:
    """Convert a unix timestamp to a UTC :class:`datetime`."""
    if value is None or value <= 0:
        return None
    return datetime.fromtimestamp(value, tz=UTC)


__all__ = ["CLIENTS_STATS_CACHE_TTL_SECONDS", "ClientsStatsService"]
