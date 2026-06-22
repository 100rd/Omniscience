"""Global reconciler for cross-store convergence.

Phase 5: Blocks reads until Neo4j and Qdrant checkpoints converge
with the Postgres Source-of-Truth watermark.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import structlog
from omniscience_core.db.models import Document
from omniscience_index.stores.qdrant_filters import QdrantFilterBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)


class GlobalReconciler:
    """Blocks until Qdrant and Neo4j checkpoints reach the Postgres watermark."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        vector_store: Any,
        graph_store: Any,
    ) -> None:
        self._session_factory = session_factory
        self._vector_store = vector_store
        self._graph_store = graph_store

    async def wait_for_convergence(self, workspace_id: uuid.UUID, timeout: float = 10.0) -> None:
        """Wait until Neo4j and Qdrant checkpoints catch up to Postgres SoT."""
        start_time = asyncio.get_event_loop().time()

        while True:
            if await self.check_convergence(workspace_id):
                return
            if asyncio.get_event_loop().time() - start_time > timeout:
                log.warning("global_reconciler_timeout", workspace_id=str(workspace_id))
                return
            await asyncio.sleep(0.5)

    async def check_convergence(self, workspace_id: uuid.UUID) -> bool:
        """Return True if all stores have reached the Postgres watermark."""
        # 1. Get Postgres watermark (max doc_version per source_id)
        async with self._session_factory() as session:
            stmt = (
                select(Document.source_id, func.max(Document.doc_version))
                .join(Document.source)
                .where(Document.source.tenant_id == workspace_id)
                .group_by(Document.source_id)
            )
            result = await session.execute(stmt)
            pg_watermarks = {str(row[0]): int(row[1] or 0) for row in result.all()}

        if not pg_watermarks:
            return True

        # 2. Get Qdrant checkpoints
        qdrant_checkpoints = await self._get_qdrant_checkpoints(workspace_id)

        # 3. Get Neo4j checkpoints
        neo4j_checkpoints = await self._get_neo4j_checkpoints(workspace_id)

        # 4. Compare
        for source_id, pg_version in pg_watermarks.items():
            if pg_version == 0:
                continue
            q_version = qdrant_checkpoints.get(source_id, 0)
            n_version = neo4j_checkpoints.get(source_id, 0)
            if q_version < pg_version or n_version < pg_version:
                return False

        return True

    async def _get_qdrant_checkpoints(self, workspace_id: uuid.UUID) -> dict[str, int]:
        """Fetch all checkpoint versions from Qdrant for a workspace."""
        try:
            flt = QdrantFilterBuilder(workspace_id=workspace_id).with_checkpoint().build()
            # _qc is the async Qdrant client inside QdrantVectorStore
            records, _ = await self._vector_store._qc.scroll(
                collection_name=self._vector_store.collection_name,
                scroll_filter=flt,
                limit=10_000,
                with_payload=["source_id", "version"],
                with_vectors=False,
            )
            return {
                str(r.payload.get("source_id")): int(r.payload.get("version", 0))
                for r in records
                if r.payload
            }
        except Exception as e:
            log.warning("global_reconciler_qdrant_error", error=str(e))
            return {}

    async def _get_neo4j_checkpoints(self, workspace_id: uuid.UUID) -> dict[str, int]:
        """Fetch all checkpoint versions from Neo4j for a workspace."""
        try:
            query = (
                "MATCH (c:StoreCheckpoint {workspace_id: $workspace_id}) "
                "RETURN c.source_id AS source_id, c.version AS version"
            )
            async with self._graph_store._driver.session(
                database=self._graph_store._config.database
            ) as session:
                records = await session.run(query, {"workspace_id": str(workspace_id)})
                return {
                    str(record["source_id"]): int(record["version"])
                    async for record in records
                }
        except Exception as e:
            log.warning("global_reconciler_neo4j_error", error=str(e))
            return {}
