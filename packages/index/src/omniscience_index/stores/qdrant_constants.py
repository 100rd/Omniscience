"""Named constants for the Qdrant vector store adapter.

Every value here is traced back to
``docs/decisions/0006-qdrant-as-vector-store.md`` (ADR-0006).  No
magic numbers are allowed in ``qdrant_store.py``; anything tunable
that the ADR names explicitly lives here with a docstring that cites
the ADR section.
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# HNSW parameters — ADR-0006, Decision §Index
# ---------------------------------------------------------------------------

#: HNSW connection graph degree.  ADR-0006, Decision:
#: "m=16, ef_construct=128 as baselines".  Memory is roughly linear in
#: ``m`` per point; 16 is the community default for general-purpose
#: retrieval workloads.
HNSW_M: Final[int] = 16

#: HNSW index-time candidate list size.  ADR-0006, Decision: "m=16,
#: ef_construct=128 as baselines".  Higher values yield a denser graph
#: at build time; 128 is the community default.
HNSW_EF_CONSTRUCT: Final[int] = 128

# ---------------------------------------------------------------------------
# Collection layout — ADR-0006, Decision §Schema posture
# ---------------------------------------------------------------------------

#: Name of the dense named vector inside every collection.  ADR-0006,
#: Decision §Schema posture: "the more common pattern will be one named
#: vector per collection and one collection per model".  The name is
#: fixed so the retrieval side does not need to introspect the schema.
NAMED_VECTOR_DENSE_PRIMARY: Final[str] = "dense_primary"

#: Name of the sparse named vector added in Stage 3
#: (refactor/bitemporal-vector-hybrid).  Carries BM25-compatible
#: IDF-weighted term frequencies for the chunk text.  Qdrant's native
#: ``SparseVectorParams`` stores the sparse index on-disk; queries
#: use ``query_points(using=NAMED_VECTOR_SPARSE_BM25, ...)`` with a
#: ``SparseVector(indices=[...], values=[...])`` query.
#:
#: Choice rationale (ADR-0004 amendment §Stage-3): we use a
#: client-side term-frequency tokenizer (split on non-alphanumeric,
#: lower-cased, stopword-filtered) rather than the Qdrant FASTEMBED
#: sparse encoder.  Reasons: (a) FASTEMBED adds a large model download
#: and GPU dependency; (b) TF-IDF on plain tokens approximates BM25
#: well enough for our use case; (c) the tokenizer is deterministic and
#: testable without a container.  The IDF weights are corpus-level
#: approximations computed client-side (not true corpus IDF, which
#: would require a full-corpus pass).  For the intended use case
#: (exact-name lookup, error strings) plain TF is sufficient because
#: rare tokens already have high discriminative power.
NAMED_VECTOR_SPARSE_BM25: Final[str] = "sparse_bm25"

#: Prefix applied to every Omniscience-managed collection.  Keeps our
#: collections distinguishable from any that might be created by other
#: tenants sharing the cluster.
COLLECTION_NAME_PREFIX: Final[str] = "omniscience"

# ---------------------------------------------------------------------------
# Payload field names — ADR-0006, Decision §Schema posture §Required payload
# ---------------------------------------------------------------------------

PAYLOAD_WORKSPACE_ID: Final[str] = "workspace_id"
PAYLOAD_SOURCE_ID: Final[str] = "source_id"
PAYLOAD_DOCUMENT_ID: Final[str] = "document_id"
PAYLOAD_EMBEDDING_MODEL: Final[str] = "embedding_model"
PAYLOAD_EMBEDDING_PROVIDER: Final[str] = "embedding_provider"
PAYLOAD_PARSER_VERSION: Final[str] = "parser_version"
PAYLOAD_CHUNKER_STRATEGY: Final[str] = "chunker_strategy"
PAYLOAD_CONTENT_TYPE: Final[str] = "content_type"
PAYLOAD_TAGS: Final[str] = "tags"
PAYLOAD_TEXT: Final[str] = "text"
PAYLOAD_ORD: Final[str] = "ord"
PAYLOAD_SYMBOL: Final[str] = "symbol"
PAYLOAD_METADATA: Final[str] = "metadata"
PAYLOAD_INGESTION_RUN_ID: Final[str] = "ingestion_run_id"
PAYLOAD_EXTERNAL_ID: Final[str] = "external_id"
PAYLOAD_URI: Final[str] = "uri"
PAYLOAD_TITLE: Final[str] = "title"
PAYLOAD_CONTENT_HASH: Final[str] = "content_hash"
PAYLOAD_DOC_VERSION: Final[str] = "doc_version"
PAYLOAD_TOMBSTONED_AT: Final[str] = "tombstoned_at"
PAYLOAD_RECORDED_AT: Final[str] = "recorded_at"
#: Bitemporal validity-window start on a chunk payload — ADR-0008 §1 +
#: §6, issue #134.  Populated on every chunk upsert (writer path);
#: defaults to ``recorded_at`` when the connector does not supply an
#: explicit ``valid_from``.  Open-closed `[valid_from, valid_to)` per
#: ADR-0008 §1; the canonical ``as_of`` predicate per ADR-0008 §5 is
#: ``valid_from <= $as_of AND ($as_of < valid_to OR valid_to IS NULL)``
#: applied as an additional ``must`` clause via :class:`QdrantFilterBuilder`.
PAYLOAD_VALID_FROM: Final[str] = "valid_from"
#: Bitemporal validity-window end on a chunk payload — ADR-0008 §6 +
#: issue #137.  Set by ``QdrantVectorStore.end_date_chunks`` instead of
#: hard-deleting points when ``GRAPH_BITEMPORAL=enabled``.  Absent /
#: ``null`` means "still valid" — the same sentinel ADR-0008 §1 fixes
#: for the graph side, mirrored on the vector store per ADR-0006 §6
#: cross-store alignment.  Only the retention worker (#135 / ADR-0009)
#: hard-deletes points; this field never triggers eviction by itself.
PAYLOAD_VALID_TO: Final[str] = "valid_to"
#: Tier marker on the chunk payload — ADR-0009 §5 / §1.  Values:
#:   "hot"     — live chunk (no `snapshot_date`).
#:   "warm"    — snapshotted chunk (carries `snapshot_date`).
#: Archive chunks are evicted from Qdrant entirely (re-embedding from
#: archive is not supported in v1 per ADR-0009 §5).
PAYLOAD_TIER: Final[str] = "tier"
#: Snapshot date on warm-tier chunks — ISO-8601 calendar day, mirrors
#: the Neo4j `:EntitySnapshot:Daily.snapshot_date` field.  Only present
#: when ``tier == "warm"``.
PAYLOAD_SNAPSHOT_DATE: Final[str] = "snapshot_date"
#: Tier values, kept in named constants so reviewers can grep them.
TIER_HOT: Final[str] = "hot"
TIER_WARM: Final[str] = "warm"

# ---------------------------------------------------------------------------
# Enumerate payload index fields — Stage 4 (refactor/bitemporal-vector-hybrid)
# ---------------------------------------------------------------------------
#
# Enumerate-mode queries ("list all X / count all Y") bypass HNSW and
# use Qdrant's ``count(exact=True)`` + ``scroll`` on payload indexes.
# The fields below are common metadata dimensions for infrastructure
# resources.  They live under the "metadata" nested payload object, so
# Qdrant accesses them as "metadata.service", "metadata.kind", etc.
#
# These indexes are additive to INDEXED_PAYLOAD_FIELDS — they do not
# replace any existing index.

#: Enumerate-dimension keys for ``metadata.*`` sub-fields.
#: Each key maps to a Qdrant KEYWORD payload index on
#: ``metadata.<key>``.  The full payload path is:
#:   metadata.service, metadata.resource_type, metadata.account_id,
#:   metadata.kind, metadata.namespace.
ENUMERATE_METADATA_INDEX_KEYS: Final[tuple[str, ...]] = (
    "service",
    "resource_type",
    "account_id",
    "kind",
    "namespace",
)

#: Payload path prefix for metadata sub-field indexes.
ENUMERATE_METADATA_PREFIX: Final[str] = "metadata"

#: Full payload paths for each enumerate-dimension index.
ENUMERATE_PAYLOAD_INDEXES: Final[tuple[str, ...]] = tuple(
    f"{ENUMERATE_METADATA_PREFIX}.{k}" for k in ENUMERATE_METADATA_INDEX_KEYS
)

# ---------------------------------------------------------------------------
# RRF parameters — Stage 3 (refactor/bitemporal-vector-hybrid)
# ---------------------------------------------------------------------------

#: Reciprocal rank fusion constant.  The standard literature (Cormack,
#: Clarke, Buettcher 2009) recommends k=60.  Higher k smooths the
#: difference between rank 1 and rank 2; lower k amplifies it.  60 is
#: the standard default for hybrid RAG pipelines and matches LangChain's
#: EnsembleRetriever default.
RRF_K: Final[int] = 60

#: Number of BM25 (sparse) candidates to retrieve before RRF fusion.
#: Widening beyond top_k gives RRF more headroom; 2x top_k is a
#: common default that keeps the sparse-side scan bounded.
SPARSE_CANDIDATE_FACTOR: Final[int] = 2

# ---------------------------------------------------------------------------
# Indexed payload fields — ADR-0006, Decision §Schema posture
# ---------------------------------------------------------------------------

#: Fields that get a Qdrant payload index.  ADR-0006, Decision §Schema
#: posture lists them explicitly — workspace_id is the mandatory one
#: (§ACL carry-forward), the rest are performance-oriented for the
#: filter-then-ANN pattern.
INDEXED_PAYLOAD_FIELDS: Final[tuple[str, ...]] = (
    PAYLOAD_WORKSPACE_ID,
    PAYLOAD_SOURCE_ID,
    PAYLOAD_DOCUMENT_ID,
    PAYLOAD_EMBEDDING_MODEL,
    PAYLOAD_EMBEDDING_PROVIDER,
    PAYLOAD_PARSER_VERSION,
    PAYLOAD_CHUNKER_STRATEGY,
    PAYLOAD_CONTENT_TYPE,
    PAYLOAD_TAGS,
    PAYLOAD_TOMBSTONED_AT,
    PAYLOAD_RECORDED_AT,
    # ADR-0008 §6 + issue #134: payload-indexed `valid_from` / `valid_to`
    # so the bitemporal `as_of` predicate is index-backed on every read.
    # `recorded_at` is the writer / retention timestamp; `valid_from` /
    # `valid_to` are the validity window the open-closed predicate hits.
    PAYLOAD_VALID_FROM,
    PAYLOAD_VALID_TO,
    # ADR-0009 §5: payload-indexed `tier` so hot reads add a cheap
    # `tier = "hot"` filter without an HNSW scan over warm payload-only
    # points.  Snapshot-date is indexed too so warm reads at a given
    # day are point-lookups.
    PAYLOAD_TIER,
    PAYLOAD_SNAPSHOT_DATE,
)

# ---------------------------------------------------------------------------
# Client transport — ADR-0006, Decision §Transport
# ---------------------------------------------------------------------------

#: Default Qdrant host used when the config does not override it.
DEFAULT_QDRANT_HOST: Final[str] = "localhost"

#: gRPC port.  ADR-0006, Decision §Transport: "gRPC primary (port
#: 6334)".
DEFAULT_QDRANT_GRPC_PORT: Final[int] = 6334

#: HTTP / REST port.  ADR-0006, Decision §Transport: "HTTP fallback
#: (port 6333)".
DEFAULT_QDRANT_HTTP_PORT: Final[int] = 6333


__all__ = [
    "COLLECTION_NAME_PREFIX",
    "DEFAULT_QDRANT_GRPC_PORT",
    "DEFAULT_QDRANT_HOST",
    "DEFAULT_QDRANT_HTTP_PORT",
    "ENUMERATE_METADATA_INDEX_KEYS",
    "ENUMERATE_METADATA_PREFIX",
    "ENUMERATE_PAYLOAD_INDEXES",
    "HNSW_EF_CONSTRUCT",
    "HNSW_M",
    "INDEXED_PAYLOAD_FIELDS",
    "NAMED_VECTOR_DENSE_PRIMARY",
    "NAMED_VECTOR_SPARSE_BM25",
    "PAYLOAD_CHUNKER_STRATEGY",
    "PAYLOAD_CONTENT_HASH",
    "PAYLOAD_CONTENT_TYPE",
    "PAYLOAD_DOCUMENT_ID",
    "PAYLOAD_DOC_VERSION",
    "PAYLOAD_EMBEDDING_MODEL",
    "PAYLOAD_EMBEDDING_PROVIDER",
    "PAYLOAD_EXTERNAL_ID",
    "PAYLOAD_INGESTION_RUN_ID",
    "PAYLOAD_METADATA",
    "PAYLOAD_ORD",
    "PAYLOAD_PARSER_VERSION",
    "PAYLOAD_RECORDED_AT",
    "PAYLOAD_SNAPSHOT_DATE",
    "PAYLOAD_SOURCE_ID",
    "PAYLOAD_SYMBOL",
    "PAYLOAD_TAGS",
    "PAYLOAD_TEXT",
    "PAYLOAD_TIER",
    "PAYLOAD_TITLE",
    "PAYLOAD_TOMBSTONED_AT",
    "PAYLOAD_URI",
    "PAYLOAD_VALID_FROM",
    "PAYLOAD_VALID_TO",
    "PAYLOAD_WORKSPACE_ID",
    "RRF_K",
    "SPARSE_CANDIDATE_FACTOR",
    "TIER_HOT",
    "TIER_WARM",
]
