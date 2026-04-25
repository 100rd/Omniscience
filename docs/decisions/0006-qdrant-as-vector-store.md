# ADR 0006 — Qdrant as vector store

- **Status**: Implemented
- **Date**: 2026-04-22
- **Implemented**: 2026-04-24 (Epic #96 Phase 5 cutover, #105)
- **Amends**: [ADR-0004](0004-retrieval-strategy-staged.md) on the vector-store-choice dimension only. The staged-retrieval strategy (hybrid → structural → GraphRAG-if-needed) in ADR-0004 still holds; this ADR replaces the pgvector substrate underneath it.

## Implementation notes

This ADR landed across three PRs:

- **[#103](https://github.com/100rd/Omniscience/issues/103)** — introduced the backend-neutral `VectorStore` protocol and wrapped the legacy pgvector writer behind a feature flag. No behaviour change.
- **[#106](https://github.com/100rd/Omniscience/issues/106)** — landed `QdrantVectorStore` as the Phase-2b vector backend with collection bootstrap and payload indexes, behind `STORAGE_VECTOR_BACKEND=qdrant`.
- **[#107](https://github.com/100rd/Omniscience/issues/107)** — `GraphRAGComposer` routed vector search through `VectorStore.search(request, workspace_id=…)`; workspace scoping is now enforced natively in the adapter via a Qdrant payload filter.

Phase 5 (**[#105](https://github.com/100rd/Omniscience/issues/105)**, v0.2.0) removed the pgvector adapter and made Qdrant the only supported vector backend. The `STORAGE_VECTOR_BACKEND` default flipped to `qdrant`; any other value is rejected at startup. The `embedding` column and HNSW index on the Postgres `chunks` table were dropped in alembic revision `0004`, along with the `vector` extension itself.

## Context

ADR-0004 selected pgvector HNSW for v0.1 as the vector half of the hybrid retrieval baseline. That decision was correct for the product Omniscience was then: a single-store retrieval service where co-locating embeddings with operational Postgres minimized moving parts and made joins between chunks and source metadata trivial. The v0.1 hybrid baseline shipped, the `search` MCP tool is live against pgvector, and the regression corpus is stable.

The product has since been re-scoped to the **Living Semantic Core** described in [`docs/vision.md`](../vision.md). [ADR-0005](0005-neo4j-as-graph-store.md) just landed the graph half of the storage split: Neo4j owns topology, ownership, dependencies, and temporal state. Postgres is retained for operational metadata only (sources, ingestion runs, auth tokens, workspaces, locks). This ADR covers the **vector** half of the same split.

Three independent forces make pgvector no longer the right substrate once ADR-0005 lands:

1. **GraphRAG composition shifts the read pattern.** Vision §5.1 specifies that the vector store is consulted *after* graph traversal narrows candidates — vector search is scoped to a set of chunk ids produced by the graph layer, not run over the whole corpus. The dominant query is "filter first, then ANN-rank," and the filter predicates are arbitrary JSON (entity kind, workspace, source, freshness window, lineage). pgvector's HNSW is competent on unfiltered kNN but filtered-search performance degrades when predicates are selective: the HNSW graph is traversed first and then rows are post-filtered, which in the worst case scans a large fraction of the index before returning top-k. Dedicated vector stores with payload-aware ANN (filter-then-search semantics) handle this workload directly.

2. **Storage coupling removes an argument that made pgvector attractive.** The original pgvector case was "one store, easy joins between chunks and source metadata." After ADR-0005, the graph is already out of Postgres. The chunks table remains in Postgres for lineage [#24](https://github.com/100rd/Omniscience/issues/24) — but the embedding column is the largest row-size contributor, drives the VACUUM and autovacuum envelope, and any HNSW index rebuild blocks operational queries. Decoupling embeddings from operational Postgres restores both stores to narrow, well-understood jobs: Postgres for transactional metadata, Qdrant for vector search with payload filtering.

3. **Per-tenant scoping under filtered search.** Vision §6 and [#117](https://github.com/100rd/Omniscience/issues/117) make workspace scoping a correctness invariant, not a performance nice-to-have. In pgvector, workspace filtering on a HNSW-indexed table with selective predicates puts pressure on the planner to choose between the HNSW index (fast kNN, post-filter) and a btree on `workspace_id` (cheap filter, then seq-scan-or-nested-loop for kNN). Neither plan is good under load. Qdrant's payload index with filter-then-HNSW is the shape this workload wants.

ADR-0004 said vector-store choice was "hybrid (pgvector + tsvector)" and was explicit that the tsvector (BM25) half would stay in Postgres. That remains true. This ADR replaces only the dense-vector substrate. The staged-retrieval decision itself — hybrid today, structural next, GraphRAG-if-needed later — is unchanged; all three strategies rebind to `(Qdrant for dense, Postgres tsvector for sparse, Neo4j for structural)` after cutover [#105](https://github.com/100rd/Omniscience/issues/105).

## Decision

**Qdrant (Community / OSS binary) is the vector store for Omniscience** starting with v0.5 (the Neo4j + Qdrant migration wave).

- **Engine**: Qdrant, written in Rust.
- **Transport**: gRPC primary (port 6334), HTTP fallback (port 6333). Client chooses gRPC by default; HTTP is available for debugging and for environments where gRPC is awkward.
- **Client**: official `qdrant-client` Python package, **async API** (`AsyncQdrantClient`). Matches the async posture already used in `packages/retrieval` and the forthcoming Neo4j adapter.
- **Index**: HNSW with payload-aware filtering. Distance metric **cosine** (matches the embedding models already shipping — see Schema posture). `m=16`, `ef_construct=128` as baselines; `ef` tuned at query time.
- **Features relied on (v0.5)**: payload-with-filter HNSW, named vectors, payload indexes, snapshots. Scalar quantization (int8) is a v0.6 optimization — not day 1 (see Consequences).
- **Edition / licensing**: Qdrant OSS binary under Apache 2.0. No Enterprise-only features are depended on.
- **Deployment shape (v0.5)**: single-node, container or Helm sub-chart, persistent volume. Suitable for v0.5 scale (≤100M vectors).
- **Deployment shape (v0.6+)**: sharding and replication on a measured trigger.

Postgres retains the `chunks` table for lineage [#24](https://github.com/100rd/Omniscience/issues/24), the `documents` table for source relationships, and tsvector BM25. Only the embedding column and its HNSW index leave Postgres after cutover.

### Why Qdrant specifically

| Criterion | Why Qdrant |
|---|---|
| Filtered-ANN performance | Payload-aware HNSW — filter predicates are evaluated as the graph is traversed, not after; selective filters stay fast |
| Client quality | Official async Python client, typed, well-maintained, ships with Pydantic models |
| Payload expressiveness | Arbitrary JSON payloads per point; payload indexes on any field; filter DSL supports AND/OR/NOT, ranges, has-key, match-any, nested fields |
| Named vectors | A single point can carry multiple embeddings — supports our Ollama-default / Voyage-fallback posture without duplicating the point |
| Operator maturity | Official [Qdrant Operator](https://qdrant.tech/documentation/guides/installation/kubernetes/) for Kubernetes and `qdrant/qdrant` Helm chart |
| Licensing | Apache 2.0 OSS binary; no Enterprise bifurcation blocking features we rely on |
| Snapshots | First-class snapshot API for offline backup; fits Kubernetes PVC snapshot story |
| Observability | Prometheus metrics endpoint, structured logs, per-collection telemetry |
| Efficiency | Rust implementation; smaller memory footprint than JVM alternatives at comparable throughput |

### Schema posture (v0.5, non-normative)

The precise schema is fixed by [#103](https://github.com/100rd/Omniscience/issues/103) (`GraphStore` / `VectorStore` interfaces) and implemented by [#106](https://github.com/100rd/Omniscience/issues/106). This ADR fixes the idioms, not the shape.

**Collection layout**: one collection **per embedding model**. A collection is defined by its vector dimensionality and distance metric, so mixing models in a single collection is not possible without named vectors. Using a named-vector collection that carries every supported model has two drawbacks: wasted space for chunks embedded by only one model, and awkward per-model filtering. Splitting by model keeps each collection internally uniform, makes re-embed after model change a collection-level operation (create new, backfill, flip the alias), and keeps the payload index small.

Workspace is **not** the collection key. Workspace is a payload field with a mandatory payload index, so a new tenant does not require a new collection. This is the opposite choice from per-workspace collections, which would multiply operational overhead without quality benefit given the expected tenant count in v0.5 (tens, not thousands).

**Named vectors within a collection**: a chunk may carry multiple embeddings when the collection is configured for it — e.g. `dense_primary` from the Ollama default model and `dense_fallback` from a secondary model — but only when both models share dimensionality and metric. The more common pattern will be one named vector per collection and one collection per model.

**Required payload fields** (per [#24](https://github.com/100rd/Omniscience/issues/24) lineage + tenancy):

| Field | Type | Indexed | Purpose |
|---|---|---|---|
| `workspace_id` | keyword (UUID) | **yes — mandatory** | Tenant boundary, enforced on every read |
| `source_id` | keyword (UUID) | yes | Source scoping, incremental re-index |
| `document_id` | keyword (UUID) | yes | Document-level grouping and deletes |
| `chunk_id` | keyword (UUID) | yes (via point id) | Stable chunk identity |
| `embedding_model` | keyword | yes | Lineage; re-embed targeting |
| `embedding_provider` | keyword | yes | Lineage |
| `parser_version` | keyword | yes | Lineage; selective re-parse |
| `chunker_strategy` | keyword | yes | Lineage |
| `content_type` | keyword | yes | Document class filter |
| `tags` | keyword[] | yes | User-supplied scoping |
| `provenance` | object | — | Ingestion run id, strategy, confidence (see §5.2 vision) |
| `valid_from`, `valid_to`, `recorded_at` | datetime | `recorded_at` indexed | Bitemporal alignment with graph (§5.3 vision) |
| `created_at`, `updated_at` | datetime | no | Operational |

**Point IDs**: chunk UUIDs, not sequential integers. Deterministic upserts, idempotent re-ingest, matches chunk identity in Postgres and Neo4j. `chunk_id` is not duplicated into the payload because the point id is already the chunk id.

**Distance**: cosine. Matches the unit-normalized embedding models we ship.

### Deployment posture

- **Development**: a single Qdrant container in `docker-compose.yml`, published on `6334` (gRPC) and `6333` (HTTP). Storage volume mounted. API key disabled in the dev profile only.
- **Helm**: add `qdrant/qdrant` as a sub-chart dependency in `helm/omniscience/Chart.yaml`, disabled by default so existing pgvector installs do not break mid-wave; enabled for v0.5 clusters. Persistent volume sized for v0.5 scale with room for 1-year warm retention (§5.3 vision) — same horizon as Neo4j.
- **Authentication**: API key mandatory in all non-dev environments, injected via Kubernetes Secret, not baked into values.
- **TLS**: enabled on both 6334 (gRPC) and 6333 (HTTP) in all non-dev environments. Cert-manager integration.
- **Snapshots**: Qdrant snapshot API invoked on schedule, uploaded to object storage. v0.5 is daily. Restore tested as part of the [#105](https://github.com/100rd/Omniscience/issues/105) cutover checklist.
- **Memory model**: HNSW graphs are held in RAM by default for query performance; scalar vectors can be memory-mapped on disk. v0.5 default is **on-disk scalar vectors, in-RAM HNSW graph** — a compromise that keeps recall high while bounding memory.
- **Resource envelope (v0.5 target)**: 4 vCPU / 32 GiB RAM / 200 GiB SSD for ≤10M vectors at 768 dims. Revisit on measured load. RAM dominates because HNSW graph size scales with point count × m × sizeof(connection).
- **Quantization (deferred)**: scalar int8 quantization reclaims roughly 4× memory at ~1–2% recall cost at sensible `ef`. v0.6 optimization, not day 1. Binary quantization is considered only for reranker-filtered pipelines where recall loss is recovered downstream; not in v0.5 scope.

### ACL carry-forward — critical

Workspace (tenant) isolation must be enforced in every Qdrant read, without exception. The same defect that ADR-0005 flagged for the graph path — [#117](https://github.com/100rd/Omniscience/issues/117), where `GraphQueryService.get_related` performs no `workspace_filter` application — has a direct analogue on the vector side: any Qdrant adapter that ships without a mandatory workspace filter on every `query_points` / `search` / `scroll` / `retrieve` / `count` call turns the migration into a new ACL bypass.

The Qdrant adapter ([#106](https://github.com/100rd/Omniscience/issues/106)) MUST:

1. **Require `workspace_id` as a non-null payload field** on every point. Upserts without it are rejected at the adapter layer.
2. **Apply a `must`-clause filter on `workspace_id`** on every read method (`search`, `query_points`, `scroll`, `retrieve`, `count`, `recommend`). No read method accepts a caller-supplied workspace; it is derived from the authenticated principal at the transport layer and threaded through as an adapter parameter.
3. **Payload-index `workspace_id`** so the filter is index-backed and selective-predicate performance is stable.
4. **Add a linter or review rule** that rejects construction of Qdrant `Filter` objects that omit `workspace_id` in the `must` clause; prefer a thin typed filter-builder that takes `workspace_id` as a required constructor argument.
5. **Audit the MCP / REST handlers** that delegate to the adapter to confirm `workspace_id` is resolved from the authenticated principal and propagated. This is coordinated with the [#117](https://github.com/100rd/Omniscience/issues/117) fix on the existing pgvector path — **both paths must enforce the same boundary before the dual-write window starts**.

Failing to do this turns the storage migration into a regression opportunity. The adapter's contract tests ([#106](https://github.com/100rd/Omniscience/issues/106)) MUST include a cross-workspace isolation test: two workspaces with overlapping entity names, a search authenticated to workspace A, and an assertion that no point from workspace B appears in any result under any combination of filters.

### Migration path

Pairs with the migration tooling ([#108](https://github.com/100rd/Omniscience/issues/108)) and the cutover ticket ([#105](https://github.com/100rd/Omniscience/issues/105)). The shape mirrors ADR-0005's migration path intentionally — one feature-flag family, one dual-write window, one cutover.

1. **Scaffold** (issues [#103](https://github.com/100rd/Omniscience/issues/103), [#106](https://github.com/100rd/Omniscience/issues/106)): `VectorStore` interface plus Qdrant adapter; adapter covered by contract tests parameterized over `(pgvector, qdrant)` so the existing baseline and the new backend run the same assertions. pgvector path untouched.
2. **Dual-write window** (via [#108](https://github.com/100rd/Omniscience/issues/108)): the writer publishes embeddings to both pgvector and Qdrant. Reads still go to pgvector. Duration: at least one full ingestion cycle per active source type, matched to the Neo4j dual-write window to keep a single cutover.
3. **Shadow reads**: retrieval layer ([#107](https://github.com/100rd/Omniscience/issues/107)) issues the Qdrant query alongside the pgvector query, compares top-k on a fixed regression corpus, logs divergence. Gate to cutover is a top-k overlap budget (fraction TBD in [#107](https://github.com/100rd/Omniscience/issues/107)); top-10 overlap is the primary metric because downstream reranking absorbs minor ordering differences.
4. **Cutover** ([#105](https://github.com/100rd/Omniscience/issues/105)): flip the read path to Qdrant, stop the pgvector dual-write, drop the `vector` columns and HNSW indexes from `chunks` in a follow-up migration; the pgvector extension is uninstalled once no schema references it.
5. **Rollback**: during the dual-write + shadow-read window, flipping back to pgvector is safe. Post-cutover rollback requires re-embedding from chunks (cheap, deterministic — embeddings are a function of `(chunk_content, embedding_model)`), not restoring a vector backup. This is a material difference from the Neo4j rollback story and is intentional: vectors are rebuildable artifacts, graph edges are not.

**Feature flag**: `retrieval_vector_backend`, values `pgvector` (default) and `qdrant`. Name chosen to match ADR-0005's `retrieval_graph_backend` family so the two flags are flipped together at cutover. Mismatched flags (graph on Neo4j, vectors on pgvector, or vice versa) are supported during the window for diagnostic purposes but not as a steady state.

## Alternatives rejected

### Stay on pgvector

Rejected now, though it was the correct choice in ADR-0004. Reasons it no longer fits:

- The primary argument for pgvector was **co-location** — chunks, metadata, and embeddings in one store with joinable semantics. ADR-0005 already moves the graph out of Postgres; the co-location argument is therefore weaker, and the operational-coupling argument gets stronger: an HNSW index rebuild or a vacuum pass on the chunks table blocks operational queries on the same database.
- **Filter-then-kNN under selective predicates** is the pattern GraphRAG will generate — graph traversal produces a set of candidate chunk ids, and the vector query filters to that set before ranking. pgvector's HNSW evaluates filters post-traversal; when the filter is selective, this is the wrong order and manifests as tail latency that a tuned HNSW was supposed to avoid.
- **Workspace scoping** on a filtered HNSW is the same problem — a payload-indexed filter-then-HNSW is the shape we want and pgvector does not offer it in 2026 releases.
- **Transaction-scope coupling** — a pgvector insert participates in the enclosing Postgres transaction. For our write pattern this is operational tax, not correctness benefit; embeddings are idempotent byproducts of chunk content and can be upserted out-of-band.

pgvector remains the right choice for workloads that are genuinely single-store, genuinely low-selectivity on the filter, and genuinely operational-Postgres-tolerant. Ours is none of those after ADR-0005.

### Weaviate

Rejected. Weaviate is a credible peer on feature set — Go-based, HNSW with payload filtering, async Python client, Kubernetes operator. Specific reasons to pass:

- **Opinionated module ecosystem**: `text2vec-*`, `generative-*`, `qna-*` modules pull embedding generation, retrieval-augmented generation, and QA composition into the store. Omniscience's design is the opposite — the store is a lean retrieval primitive, and embedding generation is explicitly a pipeline concern owned by `packages/index`. Living inside Weaviate's module assumptions would fight the architecture.
- **Hybrid search is built-in** (BM25 + dense fusion inside the store). We have already committed to tsvector for keyword (in Postgres, to retain lineage joins) and graph-first composition in Neo4j. Weaviate's hybrid would duplicate that capability without replacing either side.
- **Licensing history**: BSL-adjacent decisions around enterprise features have created churn in past years; less predictable than Qdrant's clean Apache 2.0 posture.
- **Go performance ceiling**: a fine engine, but measurements on comparable HNSW workloads consistently put Qdrant within or ahead of Weaviate on memory efficiency, which matters for our RAM-dominated envelope.

### Milvus

Rejected. Milvus (Zilliz's flagship) is the highest-scale-oriented OSS vector store, with distributed-mode credentials well beyond our v0.5 target. Specific reasons to pass:

- **Heavyweight dependencies** in distributed mode: etcd (coordination), MinIO (object storage), Pulsar (streaming). Three more distributed systems to operate for one vector store, at a scale where none of them are earning their keep.
- **Standalone mode** collapses most of that, but it also removes the distributed benefit — at which point you are running a less mature single-node Qdrant-equivalent with a larger binary and a Go+C++ toolchain.
- **Operational burden not justified** at v0.5 (≤100M vectors). Milvus becomes the right answer somewhere north of "multiple billions of vectors and a team dedicated to the store" — well outside our envelope.
- **Payload filtering**: Milvus supports scalar filters, but the DSL is less ergonomic than Qdrant's, and payload-index maturity has lagged.

### Pinecone

Rejected. Pinecone is the managed-SaaS reference implementation and has genuinely excellent filter-then-kNN performance. Specific reasons to pass:

- **Self-hosted mandate**: [`docs/vision.md`](../vision.md) §6 says "self-hosted on Kubernetes (Helm) or private SaaS on dedicated customer infrastructure. Data does not leave the customer perimeter." Pinecone's managed model violates that.
- **Pricing**: per-pod + per-index + per-read pricing compounds quickly at our expected scale; a Qdrant OSS deployment on customer-owned hardware is multiple integer factors cheaper for the target workload.
- **Data sovereignty**: pilot and design-partner customers in regulated domains (§3 target users) explicitly require on-prem. Pinecone closes that door.

### Chroma

Rejected. Chroma is a Python-native, developer-friendly store that is excellent for prototypes and single-node applications. Specific reasons to pass:

- **Single-node focus**: clustering and sharding are not primary design concerns; production operational story is thinner than Qdrant's.
- **HNSW implementation**: uses `hnswlib` under the hood; a proven library but the wrapper layer around filtering, payload indexing, and persistence is less mature than Qdrant's native implementation.
- **Filter semantics**: the `where` clause is competent but the DSL is less expressive than Qdrant's (no `match-any`, weaker nested-field support).
- **Production evidence**: fewer large-scale production deployments compared to Qdrant in 2026; the hiring and ops-knowledge pool is thinner.

### LanceDB

Rejected. LanceDB is an embedded columnar store with vector-search capability, built on the Lance format. Specific reasons to pass:

- **Embedded model** fights our architecture: ingestion is a multi-writer pipeline with concurrent workers publishing to the store, and embedded-mode concurrency (file locks, process coordination) is the wrong shape for that.
- **Newer project** (2023+), less production evidence at scale, and the moving-target nature of the Lance format is an ongoing compatibility consideration.
- **Operational story**: no Kubernetes operator in the same class as Qdrant's; no native distributed deployment.

The embedded-columnar thesis is interesting for analytical workloads; it is not the right shape for a multi-writer serving store.

### Vespa

Rejected. Vespa is mature, battle-tested at Yahoo scale, and has one of the most sophisticated ranking stacks available. Specific reasons to pass:

- **Optimized for mixed ranking** — BM25 + ML models + tensor operations composed in a single ranking expression. Powerful, and wasted on our design: keyword retrieval is already in Postgres tsvector, structural retrieval is already in Neo4j, and cross-encoder reranking (ADR-0004) is a separate pipeline stage.
- **Massive ops footprint**: application packages, content clusters, container clusters, config servers — a full Vespa deployment is several coordinated components. Justifiable at scale; overkill for v0.5.
- **Learning curve**: Vespa's schema DSL and ranking framework are deep skills; onboarding cost exceeds the value delivered for a store that will only serve dense vectors.

### Typesense and Marqo

Both rejected, briefly. Typesense is primarily a full-text search engine with vector capability added as an auxiliary feature; the primary-use-case shape is wrong. Marqo is opinionated about the ML pipeline (multimodal embedding inside the store), which is the same module-ecosystem anti-pattern that rules out Weaviate, more pronounced. Neither is a primary vector store in the sense we need.

### Apache Cassandra + StargateQL vector

Rejected. Cassandra's vector support via StargateQL is young, the filter-then-ANN story is immature relative to Qdrant, and bolting vector search onto Cassandra is a strategy that made sense only for organizations that already run Cassandra at scale — which is not us.

## Consequences

### Positive

- **Filter-then-HNSW** is first-class; selective-predicate performance is the design center of the engine, not a post-filter step after a full traversal.
- **Transaction-scope decoupling**: Postgres operational workload is no longer on the same hot path as embedding inserts and HNSW index writes.
- **Payload-index semantics** make workspace filtering, source scoping, and lineage-based targeting index-backed and predictable.
- **Named vectors** give us a single-collection story for multi-model embedding without schema contortions when the day comes for it.
- **Separation of concerns** is now complete: Neo4j owns the graph, Qdrant owns vectors, Postgres owns operational metadata and BM25. Each component has a narrow, well-understood job — the same outcome ADR-0005 listed for its side.

### Negative — operational

- **Additional datastore** in `docker-compose.yml`, Helm chart, and Kubernetes deployments. Sidecar complexity is the tax paid for the separation.
- **RAM-heavy HNSW**: v0.5 envelope sizes RAM for the HNSW graph plus page cache headroom. Quantization (v0.6) recovers roughly 4× memory at minor recall cost, but it is not a day-1 switch.
- **Snapshot strategy** needs wiring: nightly snapshot API calls, upload to object storage, tested restore path as part of the cutover checklist ([#105](https://github.com/100rd/Omniscience/issues/105)).
- **Upgrade cadence**: Qdrant has a faster release cadence than Postgres or Neo4j. Version pinning in Helm values is mandatory; upgrade runbook required before GA.

### Negative — team

- **Client API shift**: `qdrant-client` async API differs from pgvector's SQLAlchemy / asyncpg pattern. The adapter author ([#106](https://github.com/100rd/Omniscience/issues/106)) needs a short onboarding; payload-filter construction in particular has its own idioms that don't map to SQL predicates.
- **Filter DSL fluency**: Qdrant's `Filter(must=[...], should=[...], must_not=[...])` with nested conditions is a genuine second query language to keep in working memory, alongside Cypher (ADR-0005) and SQL. Mitigation: require filter construction to go through a typed filter-builder; raw `Filter` object construction outside the adapter is a review-rejected pattern.

### Negative — cost

- **RAM increase** for HNSW is the dominant cost delta. Recovered in v0.6 by scalar int8 quantization (roughly 4× memory reduction at ~1–2% recall cost).
- **Object storage** for snapshots — small relative to the RAM envelope but non-zero.
- **Network traffic**: Qdrant sits on the pod network rather than a Postgres Unix socket. Latency delta is measurable but small; bandwidth is not a concern at v0.5 scale.

### Negative — security (carried forward from #117)

- The workspace-filter bypass that [#117](https://github.com/100rd/Omniscience/issues/117) opened on the graph path is equally possible on the vector path if the Qdrant adapter ships without mandatory `workspace_id` filtering on every read. The ACL carry-forward section above is not a nice-to-have; it is an adapter contract-test invariant. The same discipline ADR-0005 mandated for the Neo4j adapter applies here verbatim.

### Risks

- **Maturity**: Qdrant is younger than pgvector. Production footprint has grown substantially through 2025–2026, but it is not pgvector-old. Monitoring long-term trajectory — release cadence, CVE response time, community health — is a standing concern for the next two releases.
- **Rust toolchain dependency** for anyone debugging the engine itself; a specialist skill on the team is not currently present. Mitigation: treat Qdrant as a black-box service; do not fork.
- **Scope creep toward generative features**: the modules Qdrant has shipped (sparse vectors, hybrid search, multivector) are interesting but out of scope for v0.5. Explicitly so. Adoption of any of them is a separate ADR.
- **Regression coverage**: the shadow-read phase requires the same fixed retrieval regression corpus ADR-0005 identified for the graph rewrite; one corpus, one cutover, shared under [#107](https://github.com/100rd/Omniscience/issues/107).

## Revisit triggers

- Vector count per collection exceeds 100M and single-node envelope runs out of headroom — evaluate Qdrant's distributed mode or reopen the Milvus question.
- Cross-cluster federation requirements emerge (multi-region with active-active vector search) — Qdrant supports sharding and replication; evaluate whether the configuration space is sufficient before looking elsewhere.
- RAM cost from HNSW becomes a budget item that quantization (v0.6) does not adequately cap — evaluate binary quantization, disk-only collections, or a hybrid-ranking pipeline that reduces the ANN candidate set.
- Licensing change in the OSS Qdrant binary — re-evaluate. Apache 2.0 is the current posture; a shift would matter.
- Maturity concerns materialize: CVE response time regression, release-cadence slip, community health decline — the window to reopen this decision is narrow but non-zero.
- Managed Qdrant Cloud pricing, data-residency, and operational envelope meet self-hosted requirements — re-evaluate deployment posture, not store choice.

## Consequences for related docs

- [`docs/vision.md`](../vision.md) §5.1 already names Qdrant; cross-link this ADR from §5.1 alongside ADR-0005.
- [`docs/architecture.md`](../architecture.md) storage section needs an update post-cutover ([#105](https://github.com/100rd/Omniscience/issues/105)) — one coordinated update covers both ADR-0005 and ADR-0006.
- [`docs/api/mcp.md`](../api/mcp.md) tool contracts are unchanged. The `search` tool signature, response shape, and lineage fields are backend-agnostic by design; only the backend swaps.
- [`docs/schema.md`](../schema.md) chunk schema reference: the `embedding` column on `chunks` is removed at cutover; lineage columns remain. Update to reflect the column drop.
- Follow-up ADR for quantization strategy (scalar int8, binary) when the v0.6 optimization lands — not in this ADR's scope.
- `docs/decisions/README.md` (or index) should list ADRs 0001–0006 in order; create or update as part of this PR if absent.

## Links

- Parent epic: [#96](https://github.com/100rd/Omniscience/issues/96)
- This issue: [#102](https://github.com/100rd/Omniscience/issues/102)
- Blocks: [#103](https://github.com/100rd/Omniscience/issues/103), [#106](https://github.com/100rd/Omniscience/issues/106)
- Depends on cutover: [#105](https://github.com/100rd/Omniscience/issues/105)
- Migration tooling: [#108](https://github.com/100rd/Omniscience/issues/108)
- Pairs with: [ADR-0005](0005-neo4j-as-graph-store.md) (Neo4j, issue [#100](https://github.com/100rd/Omniscience/issues/100))
- Amends: [ADR-0004](0004-retrieval-strategy-staged.md) on the vector-store-choice dimension only
- ACL carry-forward: [#117](https://github.com/100rd/Omniscience/issues/117)
- Lineage schema: [#24](https://github.com/100rd/Omniscience/issues/24)
