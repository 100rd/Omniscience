"""Global reconciler for cross-store convergence.

Phase 5: Blocks reads until Neo4j and Qdrant checkpoints converge
with the Postgres Source-of-Truth watermark.

AP3 (consilium-v8): surfaces explicit staleness signals instead of
silently serving stale data on timeout.  ``check_convergence`` now
snapshots the Postgres watermark once per call to prevent non-monotonic
flap when the three stores are read at different wall-clock instants.

v10-AP1 (consilium-v10): watermark is now PER-SOURCE — a cold or lagging
source only filters ITS OWN hits, not hits from healthy sources in the same
workspace.  ``check_convergence`` returns a ``per_source_watermark:
dict[str, int]`` where each entry is
``min(pg_version, neo4j_version, qdrant_version)`` for that source.  The
global ``min_watermark`` scalar is retained as the min-of-all-sources for
convenience but callers SHOULD use ``per_source_watermark`` for filtering.

``wait_for_convergence`` returns
``(degraded_subsystems, staleness_seconds, min_watermark, per_source_watermark)``.
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

    AP3 / v10-AP1 contract
    ----------------------
    ``wait_for_convergence`` returns a tuple
    ``(degraded_subsystems: list[str], staleness_seconds: float | None,
    min_watermark: int | None, per_source_watermark: dict[str, int])``.

    ``per_source_watermark`` maps each source_id (str) to the safe version
    ceiling for that source — ``min(pg_version, neo4j_version, qdrant_version)``
    computed independently for each source.  A cold source (version 0) only
    blackouts ITS OWN hits; healthy sources in the same workspace are unaffected.
    When the map is empty (no documents in workspace) no filtering is applied.

    ``min_watermark`` is the global minimum across ALL sources — retained for
    backwards compatibility and observability.  Callers MUST use
    ``per_source_watermark`` for per-hit filtering to avoid the global-blackout
    regression (v10-AP1).

    An empty ``degraded_subsystems`` means all stores converged before the
    timeout; a non-empty list names the lagging stores and
    ``staleness_seconds`` quantifies the lag of the worst offender
    (as a version-delta — not wall-clock, but proportional to the drift).

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
    ) -> tuple[list[str], float | None, int | None, dict[str, int]]:
        """Wait until Neo4j and Qdrant checkpoints catch up to the Postgres SoT.

        Returns
        -------
        ``(degraded_subsystems, staleness_seconds, min_watermark, per_source_watermark)``
            - ``degraded_subsystems``: empty on success; names the lagging
              stores on timeout (e.g. ``["neo4j"]``, ``["qdrant", "neo4j"]``).
            - ``staleness_seconds``: approximate lag in version units of the
              worst-lagging store; ``None`` when converged or unmeasurable.
            - ``min_watermark``: global minimum version across all stores and
              sources from the same snapshot.  ``None`` when no version
              information is available (e.g. cold store or no documents in the
              workspace).  Retained for observability; callers SHOULD prefer
              ``per_source_watermark`` for per-hit filtering.
            - ``per_source_watermark``: mapping of source_id (str) →
              ``min(pg, neo4j, qdrant)`` for that source.  A hit from source A
              is filtered only against ``per_source_watermark[A]``; a cold
              source B does NOT decimate hits from healthy source A.  Empty
              dict means no documents in the workspace — no filtering applied.

        Never raises — callers receive stale data with explicit signals rather
        than a hard failure.
        """
        start_time = asyncio.get_event_loop().time()

        while True:
            (
                converged,
                degraded,
                staleness,
                min_watermark,
                per_source_wm,
            ) = await self.check_convergence(workspace_id)
            if converged:
                return [], None, min_watermark, per_source_wm
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                log.warning(
                    "global_reconciler_timeout",
                    workspace_id=str(workspace_id),
                    degraded_subsystems=degraded,
                    staleness_seconds=staleness,
                    min_watermark=min_watermark,
                    per_source_watermark=per_source_wm,
                )
                return degraded, staleness, min_watermark, per_source_wm
            await asyncio.sleep(0.5)

    async def check_convergence(
        self,
        workspace_id: uuid.UUID,
    ) -> tuple[bool, list[str], float | None, int | None, dict[str, int]]:
        """Check whether all stores have reached the Postgres watermark.

        The Postgres watermark is snapshotted **once** at the start of this
        call.  Both the Qdrant and Neo4j checkpoints are then compared
        against the same snapshot, preventing non-monotonic flap caused by
        interleaved reads.

        Returns
        -------
        ``(converged, degraded_subsystems, staleness_seconds, min_watermark,
        per_source_watermark)``
            - ``converged``: True when all stores meet or exceed the PG watermark.
            - ``degraded_subsystems``: names of lagging stores (empty when converged).
            - ``staleness_seconds``: version-delta of the worst-lagging store;
              ``None`` when converged or the PG watermark is empty.
            - ``min_watermark``: global minimum version across all sources and
              all stores.  ``None`` when no version data exists.
            - ``per_source_watermark``: per-source ceiling map (v10-AP1).
              ``min(pg_v, neo4j_v, qdrant_v)`` computed independently per source.
              Used by the GraphRAG composer to filter a hit only against its own
              source's watermark — NOT a global min (v10-AP1 fix).
        """
        # 1. Snapshot the Postgres watermark once — prevents epoch-skew flap.
        pg_watermarks = await self._snapshot_pg_watermark(workspace_id)
        if not pg_watermarks:
            return True, [], None, None, {}

        # 2. Fetch store checkpoints concurrently against the same snapshot.
        qdrant_checkpoints, neo4j_checkpoints = await asyncio.gather(
            self._get_qdrant_checkpoints(workspace_id),
            self._get_neo4j_checkpoints(workspace_id),
        )

        # 3. Compare each store against the snapshot; collect lagging stores.
        qdrant_lagging = False
        neo4j_lagging = False
        max_lag: float = 0.0

        # Global version list for backwards-compatible min_watermark scalar.
        all_versions: list[int] = []

        # Per-source watermark map (v10-AP1): each source gets its own ceiling.
        per_source_watermark: dict[str, int] = {}

        for source_id, pg_version in pg_watermarks.items():
            if pg_version == 0:
                continue
            q_version = qdrant_checkpoints.get(source_id, 0)
            n_version = neo4j_checkpoints.get(source_id, 0)

            # Per-source ceiling: hits from this source are pinned to the
            # minimum version that ALL three stores agree on for it.
            source_min = min(pg_version, q_version, n_version)
            per_source_watermark[source_id] = source_min

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
            return False, degraded, max_lag, min_watermark, per_source_watermark
        return True, [], None, min_watermark, per_source_watermark

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
