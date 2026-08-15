"""FullStore: A composite store implementing StoreContract for Full-mode."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from omniscience_core.storage.contract import StoreContract
from omniscience_core.storage.graph import (
    EdgeUpsert,
    EntityNodeView,
    EntityUpsert,
    GraphResultView,
    GraphStore,
    GraphWriteResult,
)
from omniscience_core.storage.vector import (
    ChunkPayload,
    UpsertOutcome,
    VectorStore,
)


class FullStore(StoreContract):
    """Composite store delegating to distinct Vector and Graph backends."""

    def __init__(self, vector_store: VectorStore, graph_store: GraphStore) -> None:
        self._vector = vector_store
        self._graph = graph_store

    # --- VectorStore ---
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
        version: int | None = None,
        epoch: int | None = None,
        forced_replay: bool = False,
    ) -> UpsertOutcome:
        return await self._vector.upsert_chunks(
            source_id=source_id,
            external_id=external_id,
            uri=uri,
            title=title,
            content_hash=content_hash,
            metadata=metadata,
            chunks=chunks,
            ingestion_run_id=ingestion_run_id,
            version=version,
            epoch=epoch,
            forced_replay=forced_replay,
        )

    async def delete_by_document(
        self,
        *,
        source_id: uuid.UUID,
        external_id: str,
        workspace_id: uuid.UUID | None = None,
    ) -> bool:
        return await self._vector.delete_by_document(
            source_id=source_id,
            external_id=external_id,
            workspace_id=workspace_id,
        )

    async def delete_tombstoned(self, older_than: timedelta) -> int:
        return await self._vector.delete_tombstoned(older_than)

    async def search(
        self,
        *,
        request: Any,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
    ) -> Any:
        return await self._vector.search(request=request, workspace_id=workspace_id, as_of=as_of)

    async def count(
        self,
        *,
        source_id: uuid.UUID,
        as_of: datetime | None = None,
    ) -> int:
        return await self._vector.count(source_id=source_id, as_of=as_of)

    async def count_chunks(
        self,
        *,
        workspace_id: uuid.UUID,
        source_id: uuid.UUID | None = None,
        as_of: datetime | None = None,
    ) -> int:
        return await self._vector.count_chunks(
            workspace_id=workspace_id, source_id=source_id, as_of=as_of
        )

    async def count_chunks_by_source(
        self,
        *,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
    ) -> dict[str, int]:
        return await self._vector.count_chunks_by_source(workspace_id=workspace_id, as_of=as_of)

    async def enumerate_chunks(
        self,
        *,
        workspace_id: uuid.UUID,
        metadata_key: str,
        metadata_value: str,
        source_id: uuid.UUID | None = None,
    ) -> list[dict[str, Any]]:
        return await self._vector.enumerate_chunks(
            workspace_id=workspace_id,
            metadata_key=metadata_key,
            metadata_value=metadata_value,
            source_id=source_id,
        )

    async def count_enumerate(
        self,
        *,
        workspace_id: uuid.UUID,
        metadata_key: str,
        metadata_value: str,
        source_id: uuid.UUID | None = None,
    ) -> int:
        return await self._vector.count_enumerate(
            workspace_id=workspace_id,
            metadata_key=metadata_key,
            metadata_value=metadata_value,
            source_id=source_id,
        )

    # --- GraphStore ---
    async def upsert_graph(
        self,
        *,
        source_id: uuid.UUID,
        document_id: uuid.UUID,
        entities: list[Any],
        edges: list[Any],
        workspace_id: uuid.UUID,
        snapshot_at: datetime | None = None,
        version: int | None = None,
        epoch: int | None = None,
        forced_replay: bool = False,
    ) -> GraphWriteResult:
        return await self._graph.upsert_graph(
            source_id=source_id,
            document_id=document_id,
            entities=entities,
            edges=edges,
            workspace_id=workspace_id,
            snapshot_at=snapshot_at,
            version=version,
            epoch=epoch,
            forced_replay=forced_replay,
        )

    async def upsert_entity(self, *, entity: EntityUpsert, workspace_id: uuid.UUID) -> None:
        return await self._graph.upsert_entity(entity=entity, workspace_id=workspace_id)

    async def upsert_edge(self, *, edge: EdgeUpsert, workspace_id: uuid.UUID) -> None:
        return await self._graph.upsert_edge(edge=edge, workspace_id=workspace_id)

    async def upsert_edge_by_name(
        self,
        *,
        source_entity_id: uuid.UUID,
        target_name: str,
        edge_type: str,
        workspace_id: uuid.UUID,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        return await self._graph.upsert_edge_by_name(
            source_entity_id=source_entity_id,
            target_name=target_name,
            edge_type=edge_type,
            workspace_id=workspace_id,
            metadata=metadata,
        )

    async def delete_tombstoned_graph(self) -> int:
        return await self._graph.delete_tombstoned_graph()

    async def resolve_pending_stubs(self, *, workspace_id: uuid.UUID) -> int:
        return await self._graph.resolve_pending_stubs(workspace_id=workspace_id)

    async def get_entity(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
    ) -> EntityNodeView | None:
        return await self._graph.get_entity(
            entity_name=entity_name, workspace_id=workspace_id, as_of=as_of
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
        return await self._graph.find_related(
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
        return await self._graph.traverse(
            entity_name=entity_name,
            workspace_id=workspace_id,
            as_of=as_of,
            max_depth=max_depth,
            edge_types=edge_types,
        )

    async def list_entities(
        self,
        *,
        workspace_id: uuid.UUID,
        kind: str,
        cluster: str | None = None,
        name: str | None = None,
        as_of: datetime | None = None,
    ) -> list[EntityNodeView]:
        return await self._graph.list_entities(
            workspace_id=workspace_id, kind=kind, cluster=cluster, name=name, as_of=as_of
        )

    async def find_entities_by_metadata(
        self,
        *,
        key: str,
        value: Any,
        workspace_id: uuid.UUID,
    ) -> list[EntityNodeView]:
        return await self._graph.find_entities_by_metadata(
            key=key, value=value, workspace_id=workspace_id
        )

    async def get_all_entities(self, *, workspace_id: uuid.UUID) -> list[EntityNodeView]:
        return await self._graph.get_all_entities(workspace_id=workspace_id)

    async def count_entities(self, *, workspace_id: uuid.UUID) -> int:
        return await self._graph.count_entities(workspace_id=workspace_id)

    async def count_entities_by_kind(self, *, workspace_id: uuid.UUID) -> dict[str, int]:
        return await self._graph.count_entities_by_kind(workspace_id=workspace_id)

    async def count_edges_by_type(self, *, workspace_id: uuid.UUID) -> dict[str, int]:
        return await self._graph.count_edges_by_type(workspace_id=workspace_id)

    async def count_entities_by_source(self, *, workspace_id: uuid.UUID) -> dict[str, int]:
        return await self._graph.count_entities_by_source(workspace_id=workspace_id)

    async def get_entity_versions(self, *, workspace_id: uuid.UUID) -> dict[uuid.UUID, int]:
        return await self._graph.get_entity_versions(workspace_id=workspace_id)
