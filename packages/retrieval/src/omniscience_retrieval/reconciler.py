"""Global reconciler for cross-store convergence.

Phase 5: Blocks reads until Neo4j and Qdrant checkpoints converge
with the Postgres Source-of-Truth watermark.

AP3 (consilium-v8): surfaces explicit staleness signals instead of
silently serving stale data on timeout.  ``check_convergence`` now
snapshots the Postgres watermark once per call to prevent non-monotonic
flap when the three stores are read at different wall-clock instants.
``wait_for_convergence`` returns ``(degraded_subsystems, staleness_seconds,
min_watermark)`` so the GraphRAG composer can pin the read path to the
minimum observed version across all stores (AP3 consistent-stale PIN).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import structlog
from omniscience_core.db.models import Document, Source
from omniscience_index.stores.qdrant_filters import QdrantFilterBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)


class GlobalReconciler:
    """Blocks until Qdrant and Neo4j checkpoints reach the Postgres watermark.

    AP3 contract
    ------------
    ``wait_for_convergence`` returns a tuple
    ``(degraded_subsystems: list[str], staleness_seconds: float | None,
    min_watermark: int | None)``.
    An empty ``degraded_subsystems`` means all stores converged before the
    timeout; a non-empty list names the lagging stores and
    ``staleness_seconds`` quantifies the lag of the worst offender
    (as a version-delta — not wall-clock, but proportional to the drift).

    ``min_watermark`` is the minimum version seen across all three stores
    (Postgres, Neo4j, Qdrant) from the same atomic snapshot.  The GraphRAG
    composer uses it to drop any hit whose ``applied_version > min_watermark``
    so that no composed result contains evidence from a future epoch that
    some stores have not yet applied.  ``None`` means no version information
    was available (cold store or no documents) — no filter is applied.

    ``check_convergence`` snapshots the Postgres watermark **once** per
    call and compares both stores against the same snapshot, preventing
    the non-monotonic flap that occurred when watermark reads were
    interleaved with store-checkpoint reads.
    """

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

    async def wait_for_convergence(
        self,
        workspace_id: uuid.UUID,
        timeout: float = 10.0,
    ) -> tuple[list[str], float | None, int | None]:
        """Wait until Neo4j and Qdrant checkpoints catch up to the Postgres SoT.

        Returns
        -------
        ``(degraded_subsystems, staleness_seconds, min_watermark)``
            - ``degraded_subsystems``: empty on success; names the lagging
              stores on timeout (e.g. ``["neo4j"]``, ``["qdrant", "neo4j"]``).
            - ``staleness_seconds``: approximate lag in version units of the
              worst-lagging store; ``None`` when converged or unmeasurable.
            - ``min_watermark``: minimum version across all stores from the
              same snapshot.  ``None`` when no version information is available
              (e.g. cold store or no documents in the workspace).  Callers
              MUST use this to filter out hits whose ``applied_version`` exceeds
              the watermark so no mixed-epoch evidence is served.

        Never raises — callers receive stale data with explicit signals rather
        than a hard failure.
        """
        start_time = asyncio.get_event_loop().time()

        while True:
            converged, degraded, staleness, min_watermark = await self.check_convergence(
                workspace_id
            )
            if converged:
                return [], None, min_watermark
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                log.warning(
                    "global_reconciler_timeout",
                    workspace_id=str(workspace_id),
                    degraded_subsystems=degraded,
                    staleness_seconds=staleness,
                    min_watermark=min_watermark,
                )
                return degraded, staleness, min_watermark
            await asyncio.sleep(0.5)

    async def check_convergence(
        self,
        workspace_id: uuid.UUID,
    ) -> tuple[bool, list[str], float | None, int | None]:
        """Check whether all stores have reached the Postgres watermark.

        The Postgres watermark is snapshotted **once** at the start of this
        call.  Both the Qdrant and Neo4j checkpoints are then compared
        against the same snapshot, preventing non-monotonic flap caused by
        interleaved reads.

        Returns
        -------
        ``(converged, degraded_subsystems, staleness_seconds, min_watermark)``
            - ``converged``: True when all stores meet or exceed the PG watermark.
            - ``degraded_subsystems``: names of lagging stores (empty when converged).
            - ``staleness_seconds``: version-delta of the worst-lagging store;
              ``None`` when converged or the PG watermark is empty.
            - ``min_watermark``: minimum version across Postgres, Neo4j, and
              Qdrant from the same snapshot.  ``None`` when no version data
              exists.  Used by the GraphRAG composer to pin the read path
              (AP3 consistent-stale PIN).
        """
        # 1. Snapshot the Postgres watermark once — prevents epoch-skew flap.
        pg_watermarks = await self._snapshot_pg_watermark(workspace_id)
        if not pg_watermarks:
            return True, [], None, None

        # 2. Fetch store checkpoints concurrently against the same snapshot.
        qdrant_checkpoints, neo4j_checkpoints = await asyncio.gather(
            self._get_qdrant_checkpoints(workspace_id),
            self._get_neo4j_checkpoints(workspace_id),
        )

        # 3. Compare each store against the snapshot; collect lagging stores.
        qdrant_lagging = False
        neo4j_lagging = False
        max_lag: float = 0.0

        # min_watermark: minimum version seen across all three stores for
        # sources that appear in the PG watermark.  This is the "safe" epoch
        # below which all stores agree — used to pin composed results.
        all_versions: list[int] = []

        for source_id, pg_version in pg_watermarks.items():
            if pg_version == 0:
                continue
            q_version = qdrant_checkpoints.get(source_id, 0)
            n_version = neo4j_checkpoints.get(source_id, 0)

            all_versions.extend([pg_version, q_version, n_version])

            if q_version < pg_version:
                qdrant_lagging = True
                max_lag = max(max_lag, float(pg_version - q_version))
            if n_version < pg_version:
                neo4j_lagging = True
                max_lag = max(max_lag, float(pg_version - n_version))

        min_watermark: int | None = min(all_versions) if all_versions else None

        degraded: list[str] = []
        if qdrant_lagging:
            degraded.append("qdrant")
        if neo4j_lagging:
            degraded.append("neo4j")

        if degraded:
            return False, degraded, max_lag, min_watermark
        return True, [], None, min_watermark

    async def _snapshot_pg_watermark(
        self,
        workspace_id: uuid.UUID,
    ) -> dict[str, int]:
        """Read the max doc_version per source from Postgres in a single query."""
        try:
            async with self._session_factory() as session:
                stmt = (
                    select(Document.source_id, func.max(Document.doc_version))
                    .join(Source, Document.source_id == Source.id)
                    .where(Source.tenant_id == workspace_id)
                    .group_by(Document.source_id)
                )
                result = await session.execute(stmt)
                return {str(row[0]): int(row[1] or 0) for row in result.all()}
        except Exception as e:
            log.warning("global_reconciler_pg_error", error=str(e))
            return {}

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
                    str(record["source_id"]): int(record["version"]) async for record in records
                }
        except Exception as e:
            log.warning("global_reconciler_neo4j_error", error=str(e))
            return {}
