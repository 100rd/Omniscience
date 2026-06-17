# ADR 0004 — Retrieval strategy: staged (hybrid → structural → GraphRAG-if-needed)

- **Status**: Accepted
- **Date**: 2026-04-17
- **Supersedes**: none

## Context

Retrieval quality is the single biggest determinant of Omniscience's usefulness. Multiple strategies exist; none is strictly better — each solves a different class of query. A concrete decision is needed about what to implement when, to avoid either:

1. **Under-building** — ship pure vector RAG and discover too late that it misses structural queries
2. **Over-building** — implement Microsoft-style GraphRAG day one, drowning in LLM-ingestion cost before users have said what they want

## The retrieval landscape in 2026

| Strategy | What it does | Cost | When it wins |
|---|---|---|---|
| **Vector (dense)** | Embed query, kNN over chunk embeddings | Low runtime, moderate ingest | Semantic similarity ("how do I configure X") |
| **BM25 / tsvector** | Keyword scoring, exact matches | Very low | Exact names, error strings, function names |
| **Hybrid (vector + BM25)** | Both, merged via reciprocal rank fusion | Low | Baseline for production RAG |
| **Lightweight structural graph** | Follow edges already present in data (imports, DEPENDS_ON, ownerReferences, markdown links) | Very low (edges extracted deterministically) | "What depends on X?", "Where is Y used?" |
| **Re-ranking** (cross-encoder) | Second pass over top-N to boost precision | Moderate (tiny model per query) | Always a quality win for top-10 |
| **Full GraphRAG (Microsoft-style)** | LLM extracts entities + relationships, builds KG, community detection + summarisation | **Very high ingest cost** (LLM per doc × 1–many calls) | Text corpora without explicit structure (research, memos) |
| **Agentic retrieval** | LLM plans multiple retrieval calls, refines iteratively | Moderate per query | Complex queries ("why did X break?") |

**What is NOT the 2026 standard**: pure full-GraphRAG. The actual production norm is **hybrid + lightweight structural + re-ranking**, with agentic retrieval on the rise for complex queries.

## Decision

Staged implementation. Each stage delivers user-visible value; no stage commits to the next.

### v0.1 — Hybrid baseline

- Vector (pgvector HNSW) + BM25 (tsvector)
- Reciprocal rank fusion to merge
- Filters: source, type, freshness, metadata
- **Sufficient for ~70–80% of typical queries**

Already scoped in issue #13.

### v0.2 — Lightweight structural

Add edges that are **already present in source data**, extracted without LLM:

- **Code**: imports, function calls, class inheritance, module references — from tree-sitter output
- **Infrastructure**: DEPENDS_ON from Terraform state, ownerReferences from k8s, Helm chart dependencies
- **Docs**: markdown links, ADR supersedes, cross-references between pages
- **Cross-source entity linking**: match by name — Terraform resource `aws_eks_cluster.prod` ↔ k8s context `prod` ↔ Grafana dashboard `prod-cluster`; stored as weighted edges

Storage:

- Same Postgres instance, new tables: `entities` (one row per identifiable thing), `edges` (one row per relationship)
- Edge queries via recursive CTEs — or upgrade to Apache AGE if Cypher becomes useful
- **No separate graph database** (Neo4j / FalkorDB) — would add ops complexity without proportional benefit at our scale

Retrieval becomes **adaptive**: the retrieval service selects strategy based on query shape.

### v0.3+ — Optional full GraphRAG

Only if v0.2 leaves clear gaps. Triggers to consider:

- Users repeatedly ask "what does this post-mortem say about X?" type queries and answers are poor
- We accumulate text-heavy sources (ADRs, meeting notes, wikis, incident reports) with implicit relationships
- Cost-of-missed-answer outweighs LLM ingestion cost

Implementation would introduce a separate **entity extraction pipeline** (LLM over chunks) and community-detection index. Still reuses Postgres for storage.

## Adaptive retrieval

From v0.2, the `search` tool exposes an optional `retrieval_strategy` parameter:

| Value | Behavior |
|---|---|
| `"hybrid"` (default) | v0.1 hybrid — vector + BM25 |
| `"structural"` | Graph-first — interpret query as "find entities and traverse", fall back to hybrid |
| `"keyword"` | BM25-only — for exact-name lookup |
| `"auto"` | LLM classifies the query and picks strategy (light, cached) |

The **caller** is often best-placed to choose (Claude Code agent knows it's asking "what depends on X"). `auto` exists for callers that don't want to reason about it.

## Rationale for staging (not building GraphRAG now)

1. **Cost profile**: full-GraphRAG ingest adds ~$0.10–1.00 per 1000 chunks (LLM calls). For a 10M-chunk corpus — $1k–$10k per reindex. Without evidence the gain justifies this, don't build.
2. **Quality of structural edges from non-LLM sources is high** (tree-sitter, Terraform graph, k8s ownerReferences are ground truth, not inferred). Take the free signal first.
3. **User queries cluster around semantic + keyword** in practice. Exotic graph-traversal queries are valuable but rarer.
4. **Adaptive retrieval is cheap to add later** once we have multiple strategies to route between.

## Alternatives rejected

### "Ship pure vector RAG in v0.1, decide later"

Rejected. Pure vector RAG is a known production deficit — misses exact-name queries, produces off-target results for short queries. Hybrid is marginal cost and obvious win; no reason to skip.

### "Ship full GraphRAG day one"

Rejected. Premature optimisation, high cost, locks us into a specific methodology before we know whether it fits user queries. Microsoft's published numbers show gains but also dramatic ingestion costs; we'd absorb that before knowing the gain.

### "Use Neo4j as primary graph store"

Rejected for v0.2. Separate graph DB adds operational complexity. Postgres with `edges` table + recursive CTEs (or Apache AGE for Cypher) covers the structural queries we'd actually run. Revisit at v0.4+ if scale or query complexity genuinely needs it.

## Consequences

- Issue #13 (retrieval) stays as v0.1 hybrid; no graph logic added there
- v0.2 issues created for structural retrieval pieces:
  - Symbol graph for code
  - Infrastructure dependency edges
  - Cross-source entity linking
  - Adaptive retrieval agent
- `docs/api/mcp.md` documents the `retrieval_strategy` parameter now (contract), but only `hybrid` works in v0.1; other values land in v0.2
- `docs/architecture.md` gets an "Adaptive retrieval" subsection reflecting the staged plan

---

## Amendment — Stage 3: Qdrant-native sparse + RRF hybrid (refactor/bitemporal-vector-hybrid, 2026-06-10)

**Status**: Implemented (branch `refactor/bitemporal-vector-hybrid`)

### What changed

ADR-0004 specified `"hybrid" = vector + BM25` with the BM25 half staying in Postgres tsvector.
After ADR-0006 moved dense vectors to Qdrant and ADR-0005 removed the structural-Postgres path,
the tsvector BM25 half never materialised — `retrieval_strategy` accepted `"hybrid"`, `"keyword"`,
`"structural"`, and `"auto"` at the API level but all four silently dispatched to dense-only HNSW.

This amendment closes that gap: sparse BM25-compatible vectors are now stored and queried
entirely inside Qdrant alongside the dense vectors, and the `retrieval_strategy` field now
drives real dispatch.

### Implementation (Stage 3)

**Sparse vector**: A named-vector slot `sparse_bm25` is added to every Qdrant collection
(``SparseVectorParams(index=SparseIndexParams(on_disk=False))``).  Sparse vectors are built
client-side at ingest time from the chunk `text` payload:

1. Lower-case, split on `[^a-z0-9]+`.
2. Drop stop-words and single-character tokens.
3. Hash each token to a bucket index (`hash(token) % 2^20`).
4. Count term frequency; max-TF normalise to `[0, 1]`.

Choice rationale: the Qdrant FASTEMBED sparse encoder was rejected because (a) it adds a large
model download and GPU dependency, (b) plain TF on tokens is sufficient for our use case (exact
service names, error strings, k8s kinds, account IDs), and (c) the tokeniser is deterministic
and testable without a container.

**Dispatch** (``QdrantVectorStore.search``):

| `retrieval_strategy` | Qdrant query |
|---|---|
| `"keyword"` | sparse-only (`query_points(using="sparse_bm25")`) |
| `"hybrid"` (default) | RRF merge of dense + sparse |
| `"auto"` | same as `"hybrid"` |
| `"structural"` | dense-only (graph layer narrows candidates first) |

**RRF merge**: Reciprocal Rank Fusion with `k=60` (Cormack et al. 2009).
`score(d) = Σ 1/(60 + rank_i(d))` summed over dense and sparse ranked lists.
The merge is applied inside the vector store; `GraphRAGComposer._run_merge_stage`
applies graph-affinity re-scoring on top as a third signal.

`QueryStats.text_matches` is now populated with the actual sparse-hit count
(was always 0 before this amendment).

**Migration**: `scripts/reindex_qdrant_hybrid.py` backfills `sparse_bm25` vectors on existing
points that have a `text` payload but no sparse vector slot.  It is idempotent (skips already-
vectorised points) and supports `--dry-run`.  Do not run on production without a backup.

### Consequences

- `SearchRequest.retrieval_strategy` field now drives real behaviour (not just accepted and ignored).
- Clients that relied on `"keyword"` always producing the same results as `"hybrid"` must be
  updated — keyword now returns strictly sparse results, which score differently.
- The Qdrant collection schema is not backward-compatible: points ingested before Stage 3 lack
  `sparse_bm25` vectors.  Run `scripts/reindex_qdrant_hybrid.py` before enabling keyword/hybrid
  queries on an existing collection.
