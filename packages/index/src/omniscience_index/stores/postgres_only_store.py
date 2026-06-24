"""PostgresOnlyStore: A class implementing GraphStore and VectorStore protocols for Lite-mode."""

from __future__ import annotations

import contextlib
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from omniscience_core.audit.fingerprint import entity_content_hash
from omniscience_core.db.models import Chunk, Document, Edge, Entity, Source
from omniscience_core.storage.graph import (
    EdgeUpsert,
    EntityNodeView,
    EntityUpsert,
    GraphEdgeView,
    GraphResultView,
)
from omniscience_core.storage.vector import (
    ChunkPayload,
    UpsertOutcome,
)
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from omniscience_embeddings.base import EmbeddingProvider
    from omniscience_retrieval.models import (
        SearchRequest,
        SearchResult,
    )

logger = logging.getLogger(__name__)


class PostgresOnlyStore:
    """Lite-mode store that bypasses Neo4j and Qdrant entirely.
    Uses Postgres for both graph relations (recursive CTEs) and vector embeddings (pgvector).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedding_provider: EmbeddingProvider | None = None,
        degraded: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._embedding_provider = embedding_provider
        self.degraded = degraded

    async def connect(self) -> None:
        """Dynamically enable pgvector extension and add embedding column to chunks table."""
        async with self._session_factory() as session:
            try:
                await session.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                await session.execute(
                    text("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS embedding vector")
                )
                await session.commit()
            except Exception as e:
                await session.rollback()
                logger.warning("Failed to initialize vector extension/column: %s", e)

    async def close(self) -> None:
        pass

    # ------------------------------------------------------------------
    # GraphStore Write API
    # ------------------------------------------------------------------

    async def upsert_graph(
        self,
        *,
        source_id: uuid.UUID,
        document_id: uuid.UUID,
        entities: list[Any],
        edges: list[Any],
        snapshot_at: datetime | None = None,
    ) -> None:
        """Persist a batch of entities and edges for one document/source."""
        if not entities:
            return

        # workspace_id can be extracted from the first entity
        head = entities[0]
        ws = getattr(head, "workspace_id", None)
        if ws is None:
            metadata = getattr(head, "metadata", None) or {}
            ws = metadata.get("workspace_id")
        if ws is None:
            raise ValueError("upsert_graph_missing_workspace_id")
        uuid.UUID(str(ws)) if not isinstance(ws, uuid.UUID) else ws

        async with self._session_factory() as session, session.begin():
            # Standard active-state: delete existing entities/edges by source_id
            # Edge deletion cascades automatically due to FK ondelete CASCADE,
            # but we can be explicit
            entity_ids_stmt = select(Entity.id).where(Entity.source_id == source_id)
            entity_ids_res = await session.execute(entity_ids_stmt)
            entity_ids = [r[0] for r in entity_ids_res.all()]
            if entity_ids:
                await session.execute(
                    delete(Edge).where(
                        (Edge.source_entity_id.in_(entity_ids))
                        | (Edge.target_entity_id.in_(entity_ids))
                    )
                )
                await session.execute(delete(Entity).where(Entity.id.in_(entity_ids)))

            name_to_id: dict[str, uuid.UUID] = {}
            # Insert entities
            for ent in entities:
                ent_id = getattr(ent, "id", None) or uuid.uuid4()
                ent_type: str = str(
                    getattr(ent, "entity_type", None) or getattr(ent, "kind", "entity")
                )
                name: str = str(getattr(ent, "name", ""))
                display_name: str = str(getattr(ent, "display_name", name))
                chunk_id = getattr(ent, "chunk_id", None)
                metadata = getattr(ent, "metadata", None) or {}
                if isinstance(metadata, str):
                    import json

                    try:
                        metadata = json.loads(metadata)
                    except ValueError:
                        metadata = {}

                db_ent = Entity(
                    id=ent_id,
                    source_id=source_id,
                    entity_type=ent_type,
                    name=name,
                    display_name=display_name,
                    chunk_id=chunk_id,
                    entity_metadata=metadata,
                    content_hash=entity_content_hash(
                        entity_type=ent_type,
                        name=name,
                        display_name=display_name,
                        metadata=metadata,
                    ),
                )
                session.add(db_ent)
                name_to_id[name] = ent_id
                name_to_id[display_name] = ent_id

            await session.flush()

            # Insert edges
            for edge in edges:
                src_id = getattr(edge, "source_entity_id", None)
                tgt_id = getattr(edge, "target_entity_id", None)
                tgt_name = getattr(edge, "target_name", None)
                edge_type = getattr(edge, "edge_type", "calls")
                metadata = getattr(edge, "metadata", None) or {}

                if src_id is None:
                    continue

                if tgt_id is None and tgt_name is not None:
                    tgt_id = name_to_id.get(str(tgt_name))

                # Create stub target if not found
                if tgt_id is None:
                    tgt_id = uuid.uuid4()
                    stub_name = str(tgt_name or "stub")
                    stub_ent = Entity(
                        id=tgt_id,
                        source_id=source_id,
                        entity_type="stub",
                        name=stub_name,
                        display_name=stub_name,
                        entity_metadata={"is_stub": True},
                        content_hash=entity_content_hash(
                            entity_type="stub",
                            name=stub_name,
                            display_name=stub_name,
                            metadata={"is_stub": True},
                        ),
                    )
                    session.add(stub_ent)
                    await session.flush()
                    name_to_id[stub_name] = tgt_id

                if src_id == tgt_id:
                    continue  # skip self loops

                db_edge = Edge(
                    id=uuid.uuid4(),
                    source_entity_id=src_id,
                    target_entity_id=tgt_id,
                    edge_type=edge_type,
                    edge_metadata=metadata,
                )
                session.add(db_edge)

    async def upsert_entity(self, *, entity: EntityUpsert, workspace_id: uuid.UUID) -> None:
        async with self._session_factory() as session, session.begin():
            # Check if exists
            stmt = select(Entity).where(Entity.id == entity.id)
            res = await session.execute(stmt)
            db_ent = res.scalar_one_or_none()
            _hash = entity_content_hash(
                entity_type=entity.entity_type,
                name=entity.name,
                display_name=entity.display_name,
                metadata=entity.metadata,
            )
            if db_ent is None:
                db_ent = Entity(
                    id=entity.id,
                    source_id=entity.source_id,
                    entity_type=entity.entity_type,
                    name=entity.name,
                    display_name=entity.display_name,
                    chunk_id=entity.chunk_id,
                    entity_metadata=entity.metadata,
                    content_hash=_hash,
                )
                session.add(db_ent)
            else:
                db_ent.entity_type = entity.entity_type
                db_ent.name = entity.name
                db_ent.display_name = entity.display_name
                db_ent.chunk_id = entity.chunk_id
                db_ent.entity_metadata = entity.metadata
                db_ent.content_hash = _hash

    async def upsert_edge(self, *, edge: EdgeUpsert, workspace_id: uuid.UUID) -> None:
        async with self._session_factory() as session, session.begin():
            db_edge = Edge(
                id=uuid.uuid4(),
                source_entity_id=edge.source_entity_id,
                target_entity_id=edge.target_entity_id,
                edge_type=edge.edge_type,
                edge_metadata=edge.metadata,
            )
            session.add(db_edge)

    async def upsert_edge_by_name(
        self,
        *,
        source_entity_id: uuid.UUID,
        target_name: str,
        edge_type: str,
        workspace_id: uuid.UUID,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            # Find source entity to get source_id
            src_ent = await session.get(Entity, source_entity_id)
            if src_ent is None:
                return

            # Check if target entity exists in this workspace
            stmt = (
                select(Entity)
                .join(Source)
                .where(Entity.name == target_name, Source.tenant_id == workspace_id)
            )
            res = await session.execute(stmt)
            tgt_ent = res.scalar_one_or_none()
            if tgt_ent is None:
                # Create stub
                tgt_ent = Entity(
                    id=uuid.uuid4(),
                    source_id=src_ent.source_id,
                    entity_type="stub",
                    name=target_name,
                    display_name=target_name,
                    entity_metadata={"is_stub": True},
                    content_hash=entity_content_hash(
                        entity_type="stub",
                        name=target_name,
                        display_name=target_name,
                        metadata={"is_stub": True},
                    ),
                )
                session.add(tgt_ent)
                await session.flush()

            if source_entity_id == tgt_ent.id:
                return

            db_edge = Edge(
                id=uuid.uuid4(),
                source_entity_id=source_entity_id,
                target_entity_id=tgt_ent.id,
                edge_type=edge_type,
                edge_metadata=metadata or {},
            )
            session.add(db_edge)

    async def delete_tombstoned_graph(self) -> int:
        return 0

    async def resolve_pending_stubs(self, *, workspace_id: uuid.UUID) -> int:
        # Resolve stub nodes that have been replaced by real entities
        async with self._session_factory() as session, session.begin():
            stubs_stmt = (
                select(Entity)
                .join(Source)
                .where(Entity.entity_type == "stub", Source.tenant_id == workspace_id)
            )
            stubs_res = await session.execute(stubs_stmt)
            stubs = stubs_res.scalars().all()
            if not stubs:
                return 0

            count = 0
            for stub in stubs:
                real_stmt = (
                    select(Entity)
                    .join(Source)
                    .where(
                        Entity.name == stub.name,
                        Entity.entity_type != "stub",
                        Source.tenant_id == workspace_id,
                    )
                )
                real_res = await session.execute(real_stmt)
                real = real_res.scalar_one_or_none()
                if real:
                    await session.execute(
                        text(
                            "UPDATE edges SET source_entity_id = :real_id "
                            "WHERE source_entity_id = :stub_id"
                        ),
                        {"real_id": real.id, "stub_id": stub.id},
                    )
                    await session.execute(
                        text(
                            "UPDATE edges SET target_entity_id = :real_id "
                            "WHERE target_entity_id = :stub_id"
                        ),
                        {"real_id": real.id, "stub_id": stub.id},
                    )
                    await session.delete(stub)
                    count += 1
            return count

    # ------------------------------------------------------------------
    # GraphStore Read API
    # ------------------------------------------------------------------

    async def get_entity(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
    ) -> EntityNodeView | None:
        async with self._session_factory() as session:
            stmt = (
                select(Entity)
                .join(Source)
                .where(Entity.name == entity_name, Source.tenant_id == workspace_id)
            )
            res = await session.execute(stmt)
            ent = res.scalar_one_or_none()
            if ent is None:
                return None

            chunk_text = None
            if ent.chunk_id:
                chk = await session.get(Chunk, ent.chunk_id)
                if chk:
                    chunk_text = chk.text

            return EntityNodeView(
                id=ent.id,
                name=ent.name,
                kind=ent.entity_type,
                source=str(ent.source_id),
                chunk_text=chunk_text,
                display_name=ent.display_name,
                version=ent.version,
            )

    async def find_related(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
        max_depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> GraphResultView:
        return await self.traverse(
            entity_name=entity_name,
            workspace_id=workspace_id,
            as_of=as_of,
            max_depth=max_depth,
            edge_types=edge_types,
        )

    async def traverse(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
        max_depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> GraphResultView:
        async with self._session_factory() as session:
            stmt = (
                select(Entity)
                .join(Source)
                .where(Entity.name == entity_name, Source.tenant_id == workspace_id)
            )
            res = await session.execute(stmt)
            seed_ent = res.scalar_one_or_none()
            if seed_ent is None:
                raise ValueError(f"entity_not_found:{entity_name}")

            chunk_text = None
            if seed_ent.chunk_id:
                chk = await session.get(Chunk, seed_ent.chunk_id)
                if chk:
                    chunk_text = chk.text

            seed_view = EntityNodeView(
                id=seed_ent.id,
                name=seed_ent.name,
                kind=seed_ent.entity_type,
                source=str(seed_ent.source_id),
                chunk_text=chunk_text,
                depth=0,
                display_name=seed_ent.display_name,
            )

            edge_types_clause = ""
            if edge_types:
                edge_types_clause = "AND edge.edge_type = ANY(:edge_types)"

            sql = """
            WITH RECURSIVE traverse_cte AS (
                SELECT
                    e.id,
                    e.source_id,
                    e.entity_type,
                    e.name,
                    e.display_name,
                    e.chunk_id,
                    0 AS depth
                FROM entities e
                WHERE e.id = :seed_id

                UNION ALL

                SELECT
                    e.id,
                    e.source_id,
                    e.entity_type,
                    e.name,
                    e.display_name,
                    e.chunk_id,
                    tc.depth + 1 AS depth
                FROM traverse_cte tc
                JOIN edges edge ON tc.id = edge.source_entity_id
                JOIN entities e ON edge.target_entity_id = e.id
                JOIN sources s ON e.source_id = s.id
                WHERE tc.depth < :max_depth
                  AND s.tenant_id = :workspace_id
                  {edge_types_clause}
            )
            SELECT DISTINCT ON (id)
                tc.id,
                tc.source_id,
                tc.entity_type,
                tc.name,
                tc.display_name,
                tc.chunk_id,
                tc.depth,
                ch.text AS chunk_text
            FROM traverse_cte tc
            LEFT JOIN chunks ch ON tc.chunk_id = ch.id
            ORDER BY id, depth ASC;
            """
            sql = sql.replace("{edge_types_clause}", edge_types_clause)

            params = {
                "seed_id": seed_ent.id,
                "max_depth": max_depth,
                "workspace_id": workspace_id,
            }
            if edge_types:
                params["edge_types"] = edge_types

            nodes_res = await session.execute(text(sql), params)
            rows = nodes_res.all()

            related = []
            node_ids = [seed_ent.id]
            for r in rows:
                if r.id == seed_ent.id:
                    continue
                node_ids.append(r.id)
                related.append(
                    EntityNodeView(
                        id=r.id,
                        name=r.name,
                        kind=r.entity_type,
                        source=str(r.source_id),
                        chunk_text=r.chunk_text,
                        depth=r.depth,
                        display_name=r.display_name,
                    )
                )

            edges_sql = """
            SELECT
                edge.edge_type,
                src.name AS from_name,
                tgt.name AS to_name
            FROM edges edge
            JOIN entities src ON edge.source_entity_id = src.id
            JOIN entities tgt ON edge.target_entity_id = tgt.id
            WHERE edge.source_entity_id = ANY(:node_ids)
              AND edge.target_entity_id = ANY(:node_ids);
            """
            edges_res = await session.execute(text(edges_sql), {"node_ids": node_ids})
            edge_rows = edges_res.all()

            edges = [
                GraphEdgeView(
                    from_entity=er.from_name,
                    to_entity=er.to_name,
                    edge_type=er.edge_type,
                )
                for er in edge_rows
            ]

            return GraphResultView(seed=seed_view, related=related, edges=edges)

    async def list_entities(
        self,
        *,
        workspace_id: uuid.UUID,
        kind: str,
        cluster: str | None = None,
        name: str | None = None,
        as_of: datetime | None = None,
    ) -> list[EntityNodeView]:
        async with self._session_factory() as session:
            stmt = (
                select(Entity)
                .join(Source)
                .where(Entity.entity_type == kind, Source.tenant_id == workspace_id)
            )
            if name is not None:
                stmt = stmt.where(Entity.name == name)
            if cluster is not None:
                stmt = stmt.where(Entity.entity_metadata["cluster"].astext == cluster)

            res = await session.execute(stmt)
            entities = res.scalars().all()

            views = []
            for ent in entities:
                chunk_text = None
                if ent.chunk_id:
                    chk = await session.get(Chunk, ent.chunk_id)
                    if chk:
                        chunk_text = chk.text

                views.append(
                    EntityNodeView(
                        id=ent.id,
                        name=ent.name,
                        kind=ent.entity_type,
                        source=str(ent.source_id),
                        chunk_text=chunk_text,
                        display_name=ent.display_name,
                        version=ent.version,
                    )
                )
            return views

    async def find_entities_by_metadata(
        self,
        *,
        workspace_id: uuid.UUID,
        key: str,
        value: Any,
    ) -> list[EntityNodeView]:
        async with self._session_factory() as session:
            stmt = (
                select(Entity)
                .join(Source)
                .where(
                    Source.tenant_id == workspace_id,
                    Entity.entity_metadata[key].astext == str(value),
                )
            )
            res = await session.execute(stmt)
            entities = res.scalars().all()

            views = []
            for ent in entities:
                chunk_text = None
                if ent.chunk_id:
                    chk = await session.get(Chunk, ent.chunk_id)
                    if chk:
                        chunk_text = chk.text
                views.append(
                    EntityNodeView(
                        id=ent.id,
                        name=ent.name,
                        kind=ent.entity_type,
                        source=str(ent.source_id),
                        chunk_text=chunk_text,
                        display_name=ent.display_name,
                        version=ent.version,
                    )
                )
            return views

    async def get_all_entities(self, *, workspace_id: uuid.UUID) -> list[EntityNodeView]:
        async with self._session_factory() as session:
            stmt = select(Entity).join(Source).where(Source.tenant_id == workspace_id)
            res = await session.execute(stmt)
            entities = res.scalars().all()
            views = []
            for ent in entities:
                chunk_text = None
                if ent.chunk_id:
                    chk = await session.get(Chunk, ent.chunk_id)
                    if chk:
                        chunk_text = chk.text
                views.append(
                    EntityNodeView(
                        id=ent.id,
                        name=ent.name,
                        kind=ent.entity_type,
                        source=str(ent.source_id),
                        chunk_text=chunk_text,
                        display_name=ent.display_name,
                        version=ent.version,
                    )
                )
            return views

    # ------------------------------------------------------------------
    # GraphStore Stats API
    # ------------------------------------------------------------------

    async def count_entities(self, *, workspace_id: uuid.UUID) -> int:
        async with self._session_factory() as session:
            stmt = (
                select(func.count(Entity.id)).join(Source).where(Source.tenant_id == workspace_id)
            )
            res = await session.execute(stmt)
            return res.scalar() or 0

    async def count_entities_by_kind(self, *, workspace_id: uuid.UUID) -> dict[str, int]:
        async with self._session_factory() as session:
            stmt = (
                select(Entity.entity_type, func.count(Entity.id))
                .join(Source)
                .where(Source.tenant_id == workspace_id)
                .group_by(Entity.entity_type)
            )
            res = await session.execute(stmt)
            return {r[0]: r[1] for r in res.all()}

    async def count_edges_by_type(self, *, workspace_id: uuid.UUID) -> dict[str, int]:
        async with self._session_factory() as session:
            stmt = (
                select(Edge.edge_type, func.count(Edge.id))
                .join(Entity, Edge.source_entity_id == Entity.id)
                .join(Source)
                .where(Source.tenant_id == workspace_id)
                .group_by(Edge.edge_type)
            )
            res = await session.execute(stmt)
            return {r[0]: r[1] for r in res.all()}

    async def count_entities_by_source(self, *, workspace_id: uuid.UUID) -> dict[str, int]:
        async with self._session_factory() as session:
            stmt = (
                select(Entity.source_id, func.count(Entity.id))
                .join(Source)
                .where(Source.tenant_id == workspace_id)
                .group_by(Entity.source_id)
            )
            res = await session.execute(stmt)
            return {str(r[0]): r[1] for r in res.all()}

    async def get_entity_versions(
        self,
        *,
        workspace_id: uuid.UUID,
    ) -> dict[uuid.UUID, int]:
        """Return {entity_id: version} for all entities in this workspace.

        AP2 lite-mode: reads from Postgres directly.  In lite mode Postgres
        is both the SoT and the only store, so versions are always in sync.
        """
        async with self._session_factory() as session:
            stmt = (
                select(Entity.id, Entity.version)
                .join(Source)
                .where(Source.tenant_id == workspace_id)
            )
            res = await session.execute(stmt)
            return {row[0]: row[1] for row in res.all()}

    async def get_entity_content_hashes(
        self,
        *,
        workspace_id: uuid.UUID,
    ) -> dict[uuid.UUID, str]:
        """Return {entity_id: content_hash} for entities with a hash in this workspace.

        AP2 lite-mode: reads from Postgres directly.  Only entities with a
        non-NULL content_hash (written post-AP2) are returned.
        """
        async with self._session_factory() as session:
            stmt = (
                select(Entity.id, Entity.content_hash)
                .join(Source)
                .where(
                    Source.tenant_id == workspace_id,
                    Entity.content_hash.is_not(None),
                )
            )
            res = await session.execute(stmt)
            return {row[0]: row[1] for row in res.all()}

    # ------------------------------------------------------------------
    # VectorStore Write API
    # ------------------------------------------------------------------

    async def upsert_chunks(
        self,
        *,
        source_id: uuid.UUID,
        external_id: str,
        uri: str,
        title: str | None,
        content_hash: str,
        metadata: dict[str, Any],
        chunks: list[ChunkPayload],
        ingestion_run_id: uuid.UUID | None = None,
    ) -> UpsertOutcome:
        async with self._session_factory() as session, session.begin():
            doc_stmt = select(Document).where(
                Document.source_id == source_id, Document.external_id == external_id
            )
            doc_res = await session.execute(doc_stmt)
            doc = doc_res.scalar_one_or_none()
            if doc is None:
                doc = Document(
                    id=uuid.uuid4(),
                    source_id=source_id,
                    external_id=external_id,
                    uri=uri,
                    title=title,
                    content_hash=content_hash,
                    doc_metadata=metadata,
                    doc_version=1,
                )
                session.add(doc)
                await session.flush()
                action = "created"
            else:
                doc.uri = uri
                doc.title = title
                doc.content_hash = content_hash
                doc.doc_metadata = metadata
                doc.doc_version += 1
                action = "updated"

            if action == "updated":
                await session.execute(delete(Chunk).where(Chunk.document_id == doc.id))

            for chunk_payload in chunks:
                chunk = Chunk(
                    id=uuid.uuid4(),
                    document_id=doc.id,
                    ord=chunk_payload["ord"],
                    text=chunk_payload["text"],
                    symbol=chunk_payload.get("symbol"),
                    ingestion_run_id=ingestion_run_id,
                    embedding_model=chunk_payload.get("embedding_model", ""),
                    embedding_provider=chunk_payload.get("embedding_provider", ""),
                    parser_version=chunk_payload.get("parser_version", ""),
                    chunker_strategy=chunk_payload.get("chunker_strategy", ""),
                    chunk_metadata=chunk_payload.get("metadata", {}),
                )
                session.add(chunk)
                await session.flush()
                if chunk_payload.get("embedding"):
                    emb_str = "[" + ",".join(map(str, chunk_payload["embedding"])) + "]"
                    await session.execute(
                        text("UPDATE chunks SET embedding = :emb::vector WHERE id = :cid"),
                        {"emb": emb_str, "cid": chunk.id},
                    )

            return UpsertOutcome(
                action=action,
                document_id=doc.id,
                chunks_written=len(chunks),
                doc_version=doc.doc_version,
            )

    async def delete_by_document(
        self,
        *,
        source_id: uuid.UUID,
        external_id: str,
        workspace_id: uuid.UUID | None = None,
    ) -> bool:
        async with self._session_factory() as session, session.begin():
            stmt = select(Document).where(
                Document.source_id == source_id, Document.external_id == external_id
            )
            res = await session.execute(stmt)
            doc = res.scalar_one_or_none()
            if doc is None or doc.tombstoned_at is not None:
                return False
            doc.tombstoned_at = datetime.now(UTC)
            return True

    async def delete_tombstoned(self, older_than: timedelta | None = None) -> int:
        if older_than is None:
            return 0
        cutoff = datetime.now(UTC) - older_than
        async with self._session_factory() as session, session.begin():
            stmt = select(Document.id).where(Document.tombstoned_at < cutoff)
            res = await session.execute(stmt)
            doc_ids = [r[0] for r in res.all()]
            if not doc_ids:
                return 0
            await session.execute(delete(Chunk).where(Chunk.document_id.in_(doc_ids)))
            await session.execute(delete(Document).where(Document.id.in_(doc_ids)))
            return len(doc_ids)

    # ------------------------------------------------------------------
    # VectorStore Read API
    # ------------------------------------------------------------------

    async def search(
        self,
        *,
        request: SearchRequest,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
    ) -> SearchResult:
        from omniscience_retrieval.models import (
            ChunkLineage,
            Citation,
            QueryStats,
            SearchHit,
            SearchResult,
            SourceInfo,
        )

        start = time.monotonic()
        query_vector = await self._embed_query(request.query)

        async with self._session_factory() as session:
            stmt = (
                select(
                    Chunk,
                    Document,
                    Source,
                    Chunk.embedding.op("<=>")(query_vector).label("distance"),
                )
                .join(Document, Chunk.document_id == Document.id)
                .join(Source, Document.source_id == Source.id)
            )

            stmt = stmt.where(Source.tenant_id == workspace_id)

            if not request.include_tombstoned:
                stmt = stmt.where(Document.tombstoned_at.is_(None))

            if request.sources:
                source_uuids = []
                for s in request.sources:
                    with contextlib.suppress(ValueError):
                        source_uuids.append(uuid.UUID(s))
                if source_uuids:
                    stmt = stmt.where(Source.id.in_(source_uuids))

            if request.max_age_seconds:
                cutoff = datetime.now(UTC) - timedelta(seconds=request.max_age_seconds)
                stmt = stmt.where(Document.indexed_at >= cutoff)

            stmt = stmt.order_by("distance").limit(request.top_k)

            res = await session.execute(stmt)
            rows = res.all()

            hits = []
            for chk, doc, src, dist in rows:
                score = float(1.0 - dist) if dist is not None else 0.0
                if getattr(self, "degraded", False):
                    score *= 0.8  # Lite mode degraded discount

                hits.append(
                    SearchHit(
                        chunk_id=chk.id,
                        document_id=doc.id,
                        score=score,
                        text=chk.text,
                        source=SourceInfo(
                            id=src.id,
                            name=src.name,
                            type=src.source_type,
                        ),
                        citation=Citation(
                            uri=doc.uri,
                            title=doc.title,
                            indexed_at=doc.indexed_at,
                            doc_version=doc.doc_version,
                        ),
                        lineage=ChunkLineage(
                            ingestion_run_id=chk.ingestion_run_id,
                            embedding_model=chk.embedding_model,
                            embedding_provider=chk.embedding_provider,
                            parser_version=chk.parser_version,
                            chunker_strategy=chk.chunker_strategy,
                        ),
                        metadata=chk.chunk_metadata,
                        applied_version=doc.doc_version,
                        staleness=max(0.0, (datetime.now(UTC) - doc.indexed_at).total_seconds())
                        if doc.indexed_at
                        else None,
                    )
                )

            duration_ms = (time.monotonic() - start) * 1000.0

            versions = [h.applied_version for h in hits if h.applied_version is not None]
            min_applied_version = min(versions) if versions else None

            return SearchResult(
                hits=hits,
                query_stats=QueryStats(
                    total_matches_before_filters=len(hits),
                    vector_matches=len(hits),
                    text_matches=0,
                    duration_ms=duration_ms,
                ),
                min_applied_version=min_applied_version,
                effective_as_of=as_of or datetime.now(UTC),
            )

    async def _embed_query(self, query: str) -> list[float]:
        if not self._embedding_provider:
            return [0.0] * 384
        vectors = await self._embedding_provider.embed([query])
        return vectors[0]

    async def count(
        self,
        *,
        source_id: uuid.UUID,
        as_of: datetime | None = None,
    ) -> int:
        async with self._session_factory() as session:
            stmt = select(func.count(Document.id)).where(
                Document.source_id == source_id, Document.tombstoned_at.is_(None)
            )
            res = await session.execute(stmt)
            return res.scalar() or 0

    # ------------------------------------------------------------------
    # VectorStore Stats API
    # ------------------------------------------------------------------

    async def count_chunks(
        self,
        *,
        workspace_id: uuid.UUID,
        source_id: uuid.UUID | None = None,
        as_of: datetime | None = None,
    ) -> int:
        async with self._session_factory() as session:
            stmt = (
                select(func.count(Chunk.id))
                .join(Document)
                .join(Source)
                .where(Source.tenant_id == workspace_id, Document.tombstoned_at.is_(None))
            )
            if source_id:
                stmt = stmt.where(Source.id == source_id)
            res = await session.execute(stmt)
            return res.scalar() or 0

    async def count_chunks_by_source(
        self,
        *,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
    ) -> dict[str, int]:
        async with self._session_factory() as session:
            stmt = (
                select(Document.source_id, func.count(Chunk.id))
                .join(Document, Chunk.document_id == Document.id)
                .join(Source, Document.source_id == Source.id)
                .where(Source.tenant_id == workspace_id, Document.tombstoned_at.is_(None))
                .group_by(Document.source_id)
            )
            res = await session.execute(stmt)
            return {str(r[0]): r[1] for r in res.all()}

    # ------------------------------------------------------------------
    # VectorStore Enumerate API
    # ------------------------------------------------------------------

    async def enumerate_chunks(
        self,
        *,
        workspace_id: uuid.UUID,
        metadata_key: str,
        metadata_value: str,
        source_id: uuid.UUID | None = None,
    ) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            stmt = (
                select(Chunk)
                .join(Document)
                .join(Source)
                .where(
                    Source.tenant_id == workspace_id,
                    Document.tombstoned_at.is_(None),
                    Chunk.chunk_metadata[metadata_key].astext == metadata_value,
                )
            )
            if source_id:
                stmt = stmt.where(Source.id == source_id)
            res = await session.execute(stmt)
            chunks = res.scalars().all()

            seen_docs = set()
            results = []
            for chk in chunks:
                if chk.document_id not in seen_docs:
                    seen_docs.add(chk.document_id)
                    results.append(
                        {
                            "ord": chk.ord,
                            "text": chk.text,
                            "symbol": chk.symbol,
                            "metadata": chk.chunk_metadata,
                            "embedding_model": chk.embedding_model,
                            "embedding_provider": chk.embedding_provider,
                            "parser_version": chk.parser_version,
                            "chunker_strategy": chk.chunker_strategy,
                            "document_id": str(chk.document_id),
                        }
                    )
            return results

    async def count_enumerate(
        self,
        *,
        workspace_id: uuid.UUID,
        metadata_key: str,
        metadata_value: str,
        source_id: uuid.UUID | None = None,
    ) -> int:
        async with self._session_factory() as session:
            stmt = (
                select(func.count(Chunk.id))
                .join(Document)
                .join(Source)
                .where(
                    Source.tenant_id == workspace_id,
                    Document.tombstoned_at.is_(None),
                    Chunk.chunk_metadata[metadata_key].astext == metadata_value,
                )
            )
            if source_id:
                stmt = stmt.where(Source.id == source_id)
            res = await session.execute(stmt)
            return res.scalar() or 0
