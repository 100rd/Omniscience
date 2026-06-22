"""OTLP trace ingester — persists parsed traces into the index store.

Responsibilities
----------------
1. Resolve (or lazily create) a per-workspace OTel :class:`Source` row so
   that entities have somewhere to attach.  The source row is the only
   carrier of workspace identity for these entities — workspace identity
   is **always** the value passed in by the caller (route), which itself
   reads it from the authenticated bearer token.
2. Idempotent upsert of the canonical entities:

   * ``trace://{trace_id_hex}``      — kind ``otel_trace``
   * ``service://{service.name}``    — kind ``otel_service``
   * ``pod://{k8s.pod.name}``        — kind ``otel_pod``
3. Cross-source edges (``cross_ref``) from trace -> service / pod.

ACL invariant
-------------
This module never reads tenant identity from any span content.  The
``workspace_id`` argument to :meth:`OtlpIngester.ingest` is the only
authoritative tenant signal.  The route MUST derive that argument from
the authenticated :class:`ApiToken` and from no other source.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from omniscience_connectors.otel import (
    ParsedTrace,
    ParsedTraces,
    canonical_pod_name,
    canonical_service_name,
    canonical_trace_name,
)
from omniscience_core.db.models import Edge, Entity, OutboxEvent, Source, SourceStatus, SourceType
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants — kept module-level so tests can assert on the canonical contract
# ---------------------------------------------------------------------------

OTEL_SOURCE_NAME_PREFIX: str = "otel-receiver"
"""Source.name prefix; per-workspace name is ``otel-receiver:{workspace_id}``."""

OTEL_TRACE_ENTITY_TYPE: str = "otel_trace"
OTEL_SERVICE_ENTITY_TYPE: str = "otel_service"
OTEL_POD_ENTITY_TYPE: str = "otel_pod"

CROSS_REF_EDGE_TYPE: str = "cross_ref"
"""Same value as in :mod:`omniscience_index.linker` — re-declared to avoid
a cross-package import cycle in the server layer."""


class OtlpIngester:
    """Persist parsed OTLP traces into Postgres entities + edges.

    Args:
        session_factory: Async session factory to the operational metadata
            database.

    Methods are idempotent: re-ingesting the same trace updates rather
    than duplicates the entities and edges.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def ingest(self, *, workspace_id: uuid.UUID | None, parsed: ParsedTraces) -> int:
        """Persist *parsed* traces under *workspace_id*.

        Args:
            workspace_id: The authenticated token's workspace UUID, or
                ``None`` for legacy tokens.  This value is the SINGLE
                authoritative tenant signal — it is NEVER derived from
                span content.
            parsed: Decoded traces from :func:`parse_otlp_payload`.

        Returns:
            Total number of spans accepted (one per :class:`ParsedSpan`
            in the request, regardless of dedup behaviour).
        """
        if not parsed.traces:
            return 0

        async with self._session_factory() as session, session.begin():
            source = await self._get_or_create_source(session, workspace_id)
            for trace in parsed.traces.values():
                await self._upsert_trace(session, workspace_id, source.id, trace)
        return parsed.total_spans

    # ------------------------------------------------------------------
    # Source bootstrap
    # ------------------------------------------------------------------

    async def _get_or_create_source(
        self, session: AsyncSession, workspace_id: uuid.UUID | None
    ) -> Source:
        """Return the per-workspace OTel source row, creating it on first use.

        ``Source`` is one of the older tables that carries workspace
        identity in the ``tenant_id`` column (see
        ``omniscience_core.auth.workspace.workspace_filter``).  We
        scope the lookup and creation by ``tenant_id`` so each
        workspace gets its own dedicated OTel source row.
        """
        name = self._source_name(workspace_id)
        stmt = select(Source).where(
            Source.name == name,
            Source.type == SourceType.otel,
        )
        if workspace_id is not None:
            stmt = stmt.where(Source.tenant_id == workspace_id)
        existing = (await session.execute(stmt)).scalars().first()
        if existing is not None:
            return existing

        source = Source(
            name=name,
            type=SourceType.otel,
            config={"endpoint_path": "/v1/traces"},
            status=SourceStatus.active,
            tenant_id=workspace_id,
        )
        session.add(source)
        await session.flush()
        log.info(
            "otel_source_created",
            source_id=str(source.id),
            workspace_id=str(workspace_id) if workspace_id else None,
        )
        return source

    @staticmethod
    def _source_name(workspace_id: uuid.UUID | None) -> str:
        if workspace_id is None:
            return f"{OTEL_SOURCE_NAME_PREFIX}:legacy"
        return f"{OTEL_SOURCE_NAME_PREFIX}:{workspace_id}"

    # ------------------------------------------------------------------
    # Trace / service / pod entity upsert
    # ------------------------------------------------------------------

    async def _upsert_trace(
        self,
        session: AsyncSession,
        workspace_id: uuid.UUID | None,
        source_id: uuid.UUID,
        trace: ParsedTrace,
    ) -> None:
        trace_entity = await self._upsert_entity(
            session,
            workspace_id=workspace_id,
            source_id=source_id,
            entity_type=OTEL_TRACE_ENTITY_TYPE,
            name=canonical_trace_name(trace.trace_id_hex),
            display_name=trace.trace_id_hex,
            metadata={
                "trace_id": trace.trace_id_hex,
                "span_count": len(trace.spans),
                "start_unix_nano": trace.start_unix_nano,
                "end_unix_nano": trace.end_unix_nano,
                "service_names": sorted(trace.service_names),
                "pod_names": sorted(trace.pod_names),
            },
        )
        for service_name in trace.service_names:
            service_entity = await self._upsert_entity(
                session,
                workspace_id=workspace_id,
                source_id=source_id,
                entity_type=OTEL_SERVICE_ENTITY_TYPE,
                name=canonical_service_name(service_name),
                display_name=service_name,
                metadata={"service_name": service_name},
            )
            await self._upsert_edge(
                session,
                workspace_id=workspace_id,
                source_id=trace_entity.id,
                target_id=service_entity.id,
                strategy="otel_trace",
                canonical_name=canonical_trace_name(trace.trace_id_hex),
            )
        for pod_name in trace.pod_names:
            pod_entity = await self._upsert_entity(
                session,
                workspace_id=workspace_id,
                source_id=source_id,
                entity_type=OTEL_POD_ENTITY_TYPE,
                name=canonical_pod_name(pod_name),
                display_name=pod_name,
                metadata={"pod_name": pod_name},
            )
            await self._upsert_edge(
                session,
                workspace_id=workspace_id,
                source_id=trace_entity.id,
                target_id=pod_entity.id,
                strategy="otel_trace",
                canonical_name=canonical_trace_name(trace.trace_id_hex),
            )

    async def _upsert_entity(
        self,
        session: AsyncSession,
        *,
        workspace_id: uuid.UUID | None,
        source_id: uuid.UUID,
        entity_type: str,
        name: str,
        display_name: str,
        metadata: dict[str, Any],
    ) -> Entity:
        stmt = select(Entity).where(
            Entity.source_id == source_id,
            Entity.entity_type == entity_type,
            Entity.name == name,
        )
        existing = (await session.execute(stmt)).scalars().first()
        if existing is not None:
            existing.entity_metadata = {**existing.entity_metadata, **metadata}
            existing.version += 1
            event_payload = {
                "workspace_id": str(workspace_id) if workspace_id else None,
                "id": str(existing.id),
                "source_id": str(source_id),
                "entity_type": entity_type,
                "name": name,
                "display_name": display_name,
                "metadata": existing.entity_metadata,
                "version": existing.version,
            }
            session.add(OutboxEvent(event_type="entity.upsert", payload=event_payload))
            return existing

        entity = Entity(
            source_id=source_id,
            entity_type=entity_type,
            name=name,
            display_name=display_name,
            entity_metadata=metadata,
        )
        session.add(entity)
        await session.flush()

        event_payload = {
            "workspace_id": str(workspace_id) if workspace_id else None,
            "id": str(entity.id),
            "source_id": str(source_id),
            "entity_type": entity_type,
            "name": name,
            "display_name": display_name,
            "metadata": metadata,
            "version": entity.version,
        }
        session.add(OutboxEvent(event_type="entity.upsert", payload=event_payload))
        return entity

    async def _upsert_edge(
        self,
        session: AsyncSession,
        *,
        workspace_id: uuid.UUID | None,
        source_id: uuid.UUID,
        target_id: uuid.UUID,
        strategy: str,
        canonical_name: str,
    ) -> None:
        stmt = select(Edge).where(
            Edge.source_entity_id == source_id,
            Edge.target_entity_id == target_id,
            Edge.edge_type == CROSS_REF_EDGE_TYPE,
        )
        existing = (await session.execute(stmt)).scalars().first()
        if existing is not None:
            return

        edge = Edge(
            source_entity_id=source_id,
            target_entity_id=target_id,
            edge_type=CROSS_REF_EDGE_TYPE,
            edge_metadata={"strategy": strategy, "canonical_name": canonical_name},
        )
        session.add(edge)
        await session.flush()

        event_payload = {
            "workspace_id": str(workspace_id) if workspace_id else None,
            "id": str(edge.id),
            "source_entity_id": str(source_id),
            "target_entity_id": str(target_id),
            "edge_type": CROSS_REF_EDGE_TYPE,
            "metadata": edge.edge_metadata,
            "version": edge.version,
        }
        session.add(OutboxEvent(event_type="edge.upsert", payload=event_payload))


__all__ = [
    "CROSS_REF_EDGE_TYPE",
    "OTEL_POD_ENTITY_TYPE",
    "OTEL_SERVICE_ENTITY_TYPE",
    "OTEL_SOURCE_NAME_PREFIX",
    "OTEL_TRACE_ENTITY_TYPE",
    "OtlpIngester",
]
