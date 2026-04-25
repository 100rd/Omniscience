"""GraphRAG retrieval composition (issue #107, Phase 3 of Epic #96).

This module defines :class:`GraphRAGComposer`, the new retrieval entry
point that composes a ``GraphStore`` (Neo4j) and a ``VectorStore``
(Qdrant) into a single staged pipeline:

1. **Anchor stage** (optional): resolve an entity anchor in the graph
   and run :meth:`GraphStore.traverse` to collect a candidate set of
   ``source_id`` s whose subgraph is relevant to the query.
2. **Vector stage**: run :meth:`VectorStore.search` (widened by
   :data:`CANDIDATE_EXPANSION_FACTOR`) scoped — when an anchor hit —
   to that candidate source set via ``SearchRequest.sources``.
3. **Merge stage**: blend the vector score with a graph-affinity bonus
   that decays with traversal depth, then slice to ``request.top_k``.

Backwards-compatibility
-----------------------

When the injected ``graph_store`` / ``vector_store`` pair is not the
Neo4j+Qdrant stack (for example, a custom test double), the composer
delegates to the ``legacy_service`` it was constructed with — kept in
the signature for forward-compatibility with alternative backends and
for federation-peer adapters.  In the default v0.2 server wiring the
composer always lands on the GraphRAG path; the legacy branch is
unreachable at runtime.  The dispatch is **type-based** — see
:func:`_should_use_graphrag`.

ACL invariant (non-negotiable — mirrors #117 / #119 / ADR-0005 / ADR-0006)
-------------------------------------------------------------------------

- ``GraphRAGComposer.search`` requires ``workspace_id`` as a
  **keyword-only, required** parameter.  Every downstream call into
  :meth:`GraphStore.traverse` and :meth:`VectorStore.search` forwards
  it.  There is no "skip scoping" sentinel.
- The cross-workspace isolation contract test
  (``tests/test_graphrag_workspace_isolation.py``) plants data in two
  workspaces and asserts workspace A cannot see any of workspace B's
  chunks or entities through this path.

Telemetry
---------

Per-stage metrics are emitted under the ``omniscience_graphrag_*``
namespace; see :data:`_STAGE_DURATION`, :data:`_CANDIDATE_SET_SIZE`,
:data:`_ANCHOR_HIT_TOTAL`, :data:`_COMPOSED_QUERIES_TOTAL`.  Each
request also produces a structlog event ``graphrag_query_complete``
with the same numbers for trace correlation.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import structlog
from omniscience_core.storage import GraphStore
from prometheus_client import Counter, Histogram

from omniscience_retrieval.models import SearchRequest, SearchResult

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Module constants — no magic numbers in the algorithm body
# ---------------------------------------------------------------------------


#: Maximum number of BFS hops to take from an anchor entity.  Kept small
#: because the candidate set is expanded further by the vector stage;
#: deeper graphs add noise without improving recall (ADR-0005 §Graph
#: traversal budget).
MAX_ANCHOR_DEPTH: int = 2

#: Multiplier applied to ``request.top_k`` when widening the vector
#: candidate pool.  A higher value gives the graph-affinity merge more
#: re-ordering headroom, at the cost of one extra round-trip-sized
#: payload.  The value 3 matches the Qdrant adapter's default re-rank
#: oversample budget.
CANDIDATE_EXPANSION_FACTOR: int = 3

#: Maximum number of related-entity source IDs promoted from the
#: anchor subgraph into ``SearchRequest.sources``.  Caps the vector
#: filter cardinality so a hub entity (1 000+ neighbours) cannot cause
#: a filter-blowup in Qdrant.
MAX_ANCHOR_CANDIDATES: int = 32

#: Linear-blend weight between graph affinity (bonus applied when a
#: hit's source appears in the anchor subgraph) and the vector score.
#: ``merged = (1 - ALPHA) * vector + ALPHA * graph_affinity``.  ALPHA
#: is deliberately small — the vector score is the primary signal; the
#: graph affinity acts as a tie-breaker and topical anchor.
MERGE_ALPHA: float = 0.25

#: Bonus score attributed to an anchor hit at depth 1.  The bonus
#: halves for each additional hop (depth 2 -> 0.5, depth 3 -> 0.25,
#: ...) so a closely-connected entity outranks a distantly-connected
#: one even when both surface the same vector score.
GRAPH_AFFINITY_BASE: float = 1.0

#: Floor applied to the depth decay so we never multiply by zero when
#: a related entity carries ``depth == 0`` (shouldn't happen but the
#: floor is defensive).
MIN_AFFINITY: float = 0.0

#: Key in ``SearchRequest.filters`` that, when present, supplies the
#: anchor-entity name for the graph stage.  Kept out of the public
#: ``SearchRequest`` schema per issue #107 scope ("Preserve MCP search
#: contract") — the anchor is a transport-layer hint, not a model
#: field.
ANCHOR_FILTER_KEY: str = "graphrag_anchor"


# ---------------------------------------------------------------------------
# Prometheus metrics — omniscience_graphrag_* namespace
# ---------------------------------------------------------------------------


_STAGE_DURATION: Histogram = Histogram(
    name="omniscience_graphrag_stage_duration_seconds",
    documentation=(
        "Wall-clock duration of a single GraphRAG composition stage "
        "(anchor graph traversal, vector search, or merge)."
    ),
    labelnames=["stage"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

_CANDIDATE_SET_SIZE: Histogram = Histogram(
    name="omniscience_graphrag_candidate_set_size",
    documentation=(
        "Distribution of the candidate set size surfaced by the graph "
        "anchor stage (number of unique source_id values promoted into "
        "the vector search filter)."
    ),
    buckets=(0, 1, 2, 4, 8, 16, 32, 64, 128),
)

_ANCHOR_HIT_TOTAL: Counter = Counter(
    name="omniscience_graphrag_anchor_total",
    documentation=(
        "Count of GraphRAG queries by anchor outcome: 'hit' when the "
        "anchor entity resolved in the graph; 'miss' when it did not; "
        "'absent' when the caller did not supply an anchor hint."
    ),
    labelnames=["outcome"],
)

_COMPOSED_QUERIES_TOTAL: Counter = Counter(
    name="omniscience_graphrag_queries_total",
    documentation=(
        "Count of GraphRAG composed queries by dispatch path: "
        "'graphrag' when the Neo4j+Qdrant stack was used, 'legacy' "
        "when the call was forwarded to the injected legacy_service "
        "(unreachable in the default v0.2 wiring — see CHANGELOG §0.2.0)."
    ),
    labelnames=["path"],
)


# ---------------------------------------------------------------------------
# Minimal protocols consumed by the composer
# ---------------------------------------------------------------------------


class _VectorSearchCallable(Protocol):
    """Narrow protocol: any object with ``async search(request, workspace_id)``.

    Accepts :class:`QdrantVectorStore` and any forward-compatible
    vector store without importing it — keeps the module free of a
    static dependency on ``omniscience_index``.
    """

    async def search(
        self,
        *,
        request: SearchRequest,
        workspace_id: uuid.UUID,
    ) -> SearchResult: ...


class _LegacySearchCallable(Protocol):
    """Narrow protocol for the legacy fallback path.

    ``FederatedSearch.search`` satisfies this signature, as do any
    forward-compatible custom shims operators may wire.  Kept unaware
    of workspace scoping by design: the caller that falls back here
    is, by definition, running in a non-GraphRAG configuration.
    """

    async def search(self, request: SearchRequest) -> SearchResult: ...


# ---------------------------------------------------------------------------
# Dispatch decision — type-based so shadow-read deployments work
# ---------------------------------------------------------------------------


def _should_use_graphrag(
    graph_store: GraphStore,
    vector_store: Any,
) -> bool:
    """Return ``True`` iff the Neo4j+Qdrant composition path is active.

    GraphRAG activates only when **both** stores are the canonical
    Neo4j / Qdrant adapters.  Any other combination (test doubles,
    custom shims) is routed through the injected ``legacy_service``.
    In the default v0.2 wiring both stores are always the canonical
    ones, so the legacy branch is unreachable at runtime.

    Type-based rather than flag-sniffing so an operator that wires
    custom stores for a blue/green rollout can still land on the
    correct path.  Imports are lazy to avoid a retrieval ->
    index circular dependency at module import time.
    """
    try:
        from omniscience_index.stores.neo4j_store import Neo4jGraphStore
        from omniscience_index.stores.qdrant_store import QdrantVectorStore
    except ImportError:  # pragma: no cover - index package must be present
        return False
    return isinstance(graph_store, Neo4jGraphStore) and isinstance(vector_store, QdrantVectorStore)


# ---------------------------------------------------------------------------
# Stage result containers — internal, typed
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _AnchorStageResult:
    """Outcome of the graph anchor stage."""

    #: True when the caller supplied an anchor hint.
    anchor_requested: bool
    #: The anchor name passed (empty when ``anchor_requested`` is False).
    anchor_name: str
    #: True when the anchor entity resolved in the graph.
    anchor_hit: bool
    #: Candidate ``source_id`` values derived from the anchor subgraph.
    #: Order is stable and bounded by :data:`MAX_ANCHOR_CANDIDATES`.
    candidate_source_ids: tuple[str, ...]
    #: Depth of each candidate source (mirrors ``candidate_source_ids``
    #: positionally).  Depth 0 is reserved for the seed's own source;
    #: depth >= 1 for related entities.
    candidate_depths: tuple[int, ...]
    #: Wall-clock duration of this stage in seconds.
    duration_s: float


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------


class GraphRAGComposer:
    """Retrieval entry point that composes GraphStore + VectorStore.

    The public :meth:`search` contract matches ``RetrievalService``
    and ``FederatedSearch`` (same ``SearchRequest`` / ``SearchResult``
    shapes) so MCP and REST callers are unaware of the composition.

    Dispatch
    --------
    On every call :meth:`search` inspects the injected store pair via
    :func:`_should_use_graphrag`.  When the pair is Neo4j+Qdrant, the
    staged pipeline runs; otherwise the call is forwarded to
    ``legacy_service`` unchanged.

    ACL
    ---
    ``search`` requires ``workspace_id`` as a keyword-only argument;
    every downstream store call forwards it.  Cross-workspace
    isolation is covered by a contract test (issue #107 acceptance).
    """

    def __init__(
        self,
        *,
        graph_store: GraphStore,
        vector_store: _VectorSearchCallable,
        legacy_service: _LegacySearchCallable,
    ) -> None:
        self._graph_store = graph_store
        self._vector_store = vector_store
        self._legacy_service = legacy_service
        self._graphrag_active = _should_use_graphrag(graph_store, vector_store)

    @property
    def graphrag_active(self) -> bool:
        """True when the new Neo4j+Qdrant composition path is active.

        Exposed for observability / tests only; callers should not
        branch on it — the composer dispatches correctly on its own.
        """
        return self._graphrag_active

    async def search(
        self,
        request: SearchRequest,
        *,
        workspace_id: uuid.UUID,
    ) -> SearchResult:
        """Execute a GraphRAG composed search (or fall back to legacy).

        Parameters
        ----------
        request:
            The public ``SearchRequest`` model.  Callers may include
            an anchor-entity hint via ``request.filters[ANCHOR_FILTER_KEY]``;
            when absent the anchor stage is skipped and the vector
            stage runs unscoped.
        workspace_id:
            REQUIRED.  Forwarded to every downstream store call so the
            ACL invariant is transitively enforced.

        Returns
        -------
        ``SearchResult`` with the same shape the MCP ``search`` tool
        produces today.  ``query_stats.duration_ms`` reflects the
        end-to-end wall-clock time of the composed query.
        """
        if not self._graphrag_active:
            _COMPOSED_QUERIES_TOTAL.labels(path="legacy").inc()
            return await self._legacy_service.search(request)

        _COMPOSED_QUERIES_TOTAL.labels(path="graphrag").inc()
        start = time.monotonic()

        anchor = await self._run_anchor_stage(request=request, workspace_id=workspace_id)
        vector_result = await self._run_vector_stage(
            request=request,
            workspace_id=workspace_id,
            anchor=anchor,
        )
        merged = self._run_merge_stage(
            request=request,
            vector_result=vector_result,
            anchor=anchor,
        )

        duration_ms = (time.monotonic() - start) * 1000.0
        log.info(
            "graphrag_query_complete",
            workspace_id=str(workspace_id),
            anchor_requested=anchor.anchor_requested,
            anchor_hit=anchor.anchor_hit,
            candidate_count=len(anchor.candidate_source_ids),
            vector_hits=len(vector_result.hits),
            returned=len(merged.hits),
            duration_ms=duration_ms,
        )
        # Re-use the vector result's QueryStats but patch duration.
        return merged.model_copy(
            update={
                "query_stats": merged.query_stats.model_copy(update={"duration_ms": duration_ms})
            }
        )

    # ------------------------------------------------------------------
    # Stage 1 — anchor
    # ------------------------------------------------------------------

    async def _run_anchor_stage(
        self,
        *,
        request: SearchRequest,
        workspace_id: uuid.UUID,
    ) -> _AnchorStageResult:
        """Resolve the anchor entity (when supplied) and collect candidates."""
        stage_start = time.monotonic()
        anchor_name = _extract_anchor(request)

        if not anchor_name:
            _ANCHOR_HIT_TOTAL.labels(outcome="absent").inc()
            duration = time.monotonic() - stage_start
            _STAGE_DURATION.labels(stage="anchor").observe(duration)
            _CANDIDATE_SET_SIZE.observe(0)
            return _AnchorStageResult(
                anchor_requested=False,
                anchor_name="",
                anchor_hit=False,
                candidate_source_ids=(),
                candidate_depths=(),
                duration_s=duration,
            )

        try:
            graph_result = await self._graph_store.traverse(
                entity_name=anchor_name,
                workspace_id=workspace_id,
                max_depth=MAX_ANCHOR_DEPTH,
            )
        except ValueError:
            # entity_not_found -> anchor miss (indistinguishable from
            # cross-workspace by design in GraphStore contracts).
            _ANCHOR_HIT_TOTAL.labels(outcome="miss").inc()
            duration = time.monotonic() - stage_start
            _STAGE_DURATION.labels(stage="anchor").observe(duration)
            _CANDIDATE_SET_SIZE.observe(0)
            return _AnchorStageResult(
                anchor_requested=True,
                anchor_name=anchor_name,
                anchor_hit=False,
                candidate_source_ids=(),
                candidate_depths=(),
                duration_s=duration,
            )

        _ANCHOR_HIT_TOTAL.labels(outcome="hit").inc()
        candidates, depths = _collect_candidates(graph_result)
        duration = time.monotonic() - stage_start
        _STAGE_DURATION.labels(stage="anchor").observe(duration)
        _CANDIDATE_SET_SIZE.observe(len(candidates))
        return _AnchorStageResult(
            anchor_requested=True,
            anchor_name=anchor_name,
            anchor_hit=True,
            candidate_source_ids=candidates,
            candidate_depths=depths,
            duration_s=duration,
        )

    # ------------------------------------------------------------------
    # Stage 2 — vector
    # ------------------------------------------------------------------

    async def _run_vector_stage(
        self,
        *,
        request: SearchRequest,
        workspace_id: uuid.UUID,
        anchor: _AnchorStageResult,
    ) -> SearchResult:
        """Run the vector search, scoped to anchor candidates when present.

        The ``top_k`` is widened by :data:`CANDIDATE_EXPANSION_FACTOR`
        so the merge stage has enough headroom to re-order.  The
        ``sources`` filter is populated only when the anchor produced
        candidates; otherwise the request is passed through unchanged.
        """
        stage_start = time.monotonic()
        widened = _widen_request(request, anchor=anchor)
        result = await self._vector_store.search(
            request=widened,
            workspace_id=workspace_id,
        )
        duration = time.monotonic() - stage_start
        _STAGE_DURATION.labels(stage="vector").observe(duration)
        return result

    # ------------------------------------------------------------------
    # Stage 3 — merge
    # ------------------------------------------------------------------

    def _run_merge_stage(
        self,
        *,
        request: SearchRequest,
        vector_result: SearchResult,
        anchor: _AnchorStageResult,
    ) -> SearchResult:
        """Blend graph affinity into the vector score and slice to top-k.

        Deduplicates by ``chunk_id`` (a malformed adapter returning
        the same chunk twice must not inflate top-k).  Stable order is
        preserved for hits with equal merged scores.
        """
        stage_start = time.monotonic()
        affinity_by_source = _build_affinity_map(anchor)
        scored: list[tuple[float, int, Any]] = []
        seen_chunks: set[uuid.UUID] = set()
        for idx, hit in enumerate(vector_result.hits):
            if hit.chunk_id in seen_chunks:
                continue
            seen_chunks.add(hit.chunk_id)
            affinity = affinity_by_source.get(str(hit.source.id), MIN_AFFINITY)
            merged_score = _linear_blend(vector_score=hit.score, graph_affinity=affinity)
            scored.append((merged_score, idx, hit))

        # Sort: descending by score, tie-break by original index (stable).
        scored.sort(key=lambda row: (-row[0], row[1]))

        top_k = request.top_k
        top_hits = [hit.model_copy(update={"score": score}) for score, _, hit in scored[:top_k]]
        duration = time.monotonic() - stage_start
        _STAGE_DURATION.labels(stage="merge").observe(duration)
        return SearchResult(hits=top_hits, query_stats=vector_result.query_stats)


# ---------------------------------------------------------------------------
# Pure helpers (module-level, easy to unit-test)
# ---------------------------------------------------------------------------


def _extract_anchor(request: SearchRequest) -> str:
    """Read the anchor-entity hint from ``request.filters`` safely.

    Returns the empty string when no hint is present, when the hint is
    not a string, or when ``filters`` is ``None``.  Never raises — a
    malformed hint simply disables the anchor stage.
    """
    if not request.filters:
        return ""
    raw = request.filters.get(ANCHOR_FILTER_KEY)
    if not isinstance(raw, str):
        return ""
    return raw.strip()


def _collect_candidates(
    graph_result: Any,
) -> tuple[tuple[str, ...], tuple[int, ...]]:
    """Extract candidate source IDs (and their depths) from a traversal.

    The seed entity's source counts as a depth-0 candidate; related
    entities contribute their own ``depth`` (>= 1).  The output is
    de-duplicated preserving the first occurrence and capped at
    :data:`MAX_ANCHOR_CANDIDATES` to keep the downstream filter small.
    """
    seed_source = str(graph_result.seed.source)
    ids: list[str] = [seed_source]
    depths: list[int] = [0]
    seen: set[str] = {seed_source}
    for node in graph_result.related:
        src = str(node.source)
        if src in seen:
            continue
        seen.add(src)
        ids.append(src)
        depths.append(int(node.depth))
        if len(ids) >= MAX_ANCHOR_CANDIDATES:
            break
    return tuple(ids), tuple(depths)


def _widen_request(
    request: SearchRequest,
    *,
    anchor: _AnchorStageResult,
) -> SearchRequest:
    """Return a copy of ``request`` widened for the vector stage.

    - ``top_k`` is multiplied by :data:`CANDIDATE_EXPANSION_FACTOR`.
    - ``sources`` is set to the anchor candidate IDs when the anchor
      produced any; otherwise the original value is preserved.
    - The ``graphrag_anchor`` filter key is stripped before the request
      is forwarded to the vector store so downstream stores do not see
      composition-internal hints.
    """
    widened_top_k = request.top_k * CANDIDATE_EXPANSION_FACTOR
    sources: list[str] | None = (
        list(anchor.candidate_source_ids) if anchor.candidate_source_ids else request.sources
    )
    clean_filters = _strip_anchor_filter(request.filters)
    return request.model_copy(
        update={
            "top_k": widened_top_k,
            "sources": sources,
            "filters": clean_filters,
        }
    )


def _strip_anchor_filter(
    filters: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return ``filters`` with the anchor hint removed, or ``None``."""
    if not filters:
        return filters
    if ANCHOR_FILTER_KEY not in filters:
        return filters
    cleaned = {k: v for k, v in filters.items() if k != ANCHOR_FILTER_KEY}
    return cleaned or None


def _build_affinity_map(anchor: _AnchorStageResult) -> dict[str, float]:
    """Translate anchor depths into per-source affinity scores.

    Depth 1 -> :data:`GRAPH_AFFINITY_BASE`; each further hop halves
    the bonus.  Depth 0 (the seed's own source) shares the depth-1
    bonus because the seed chunk is maximally relevant.
    """
    out: dict[str, float] = {}
    for source_id, depth in zip(anchor.candidate_source_ids, anchor.candidate_depths, strict=True):
        effective_depth = max(1, depth)
        out[source_id] = GRAPH_AFFINITY_BASE / float(2 ** (effective_depth - 1))
    return out


def _linear_blend(*, vector_score: float, graph_affinity: float) -> float:
    """Compute the merged score for a single hit.

    ``merged = (1 - MERGE_ALPHA) * vector_score + MERGE_ALPHA * graph_affinity``
    """
    return (1.0 - MERGE_ALPHA) * vector_score + MERGE_ALPHA * graph_affinity


__all__ = [
    "ANCHOR_FILTER_KEY",
    "CANDIDATE_EXPANSION_FACTOR",
    "GRAPH_AFFINITY_BASE",
    "MAX_ANCHOR_CANDIDATES",
    "MAX_ANCHOR_DEPTH",
    "MERGE_ALPHA",
    "GraphRAGComposer",
]
