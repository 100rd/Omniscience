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

#: Name of the single named vector inside every collection.  ADR-0006,
#: Decision §Schema posture: "the more common pattern will be one named
#: vector per collection and one collection per model".  The name is
#: fixed so the retrieval side does not need to introspect the schema.
NAMED_VECTOR_DENSE_PRIMARY: Final[str] = "dense_primary"

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
    "HNSW_EF_CONSTRUCT",
    "HNSW_M",
    "INDEXED_PAYLOAD_FIELDS",
    "NAMED_VECTOR_DENSE_PRIMARY",
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
    "PAYLOAD_VALID_TO",
    "PAYLOAD_WORKSPACE_ID",
    "TIER_HOT",
    "TIER_WARM",
]
