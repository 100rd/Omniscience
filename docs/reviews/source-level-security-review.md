# Source-level security review — token / scope / ACL / query-construction surface

- **Date:** 2026-07-01
- **Trigger:** Consilium round-1 action point (P1): all 24 embedded priority
  files were manifests/config, so the security-critical surface was
  unverifiable from that snapshot. This review reads the implementation
  modules directly. The bundle for later rounds is pinned in
  `docs/reviews/consilium-priority-files.txt` and guarded by
  `tests/test_security_review_source_bundle_guard.py`.
- **Verdict:** the reviewed surface is secure by construction on the primary
  (Qdrant + Neo4j + GraphRAG) paths, with **three findings** (2 Medium,
  1 Low) on secondary paths — none is a cross-workspace leak on the default
  configuration, all have concrete fixes listed below.

## 1. Token hash/verify

Reviewed: `packages/core/src/omniscience_core/auth/tokens.py`,
`packages/core/src/omniscience_core/auth/middleware.py`.

- Tokens are generated from `secrets.token_urlsafe(24)` plus a UUID4 and
  hashed with argon2id (`PasswordHasher`); only the hash and an 8-char
  prefix are persisted. The plaintext is returned exactly once from
  `create_api_token` and is never logged (structlog calls emit only the
  prefix).
- `verify_token` delegates to argon2's constant-time verify and maps
  `VerifyMismatchError`/`VerificationError` to `False` — no exception
  oracle.
- `_lookup_token` in `middleware.py` selects candidates by
  `token_prefix` + `is_active`, then argon2-verifies each candidate and
  checks `expires_at` against UTC now. Revocation is a soft-delete
  (`is_active = False` in `delete_api_token`), which the lookup honours.
- Cookie-authenticated (admin SPA) mutating requests additionally require
  the double-submit CSRF header, compared with
  `secrets.compare_digest` (constant-time); Bearer requests are exempt,
  which is correct since Bearer credentials are not ambient.

No findings. Minor observation: `create_api_token` hardcodes
`env = "development"`, so the prefix's environment tag is cosmetic — not a
security issue, but worth wiring to real settings.

## 2. Per-tool scope enforcement

Reviewed: `packages/core/src/omniscience_core/auth/scopes.py`,
`apps/server/src/omniscience_server/mcp/server.py`,
`apps/server/src/omniscience_server/mcp/tools.py`.

- `check_scopes` expands the granted set through an explicit
  `SCOPE_HIERARCHY` (only `admin` implies others) and requires
  `required ⊆ effective`. Unknown scope strings on a token are dropped
  before expansion (`require_scope` in `middleware.py` filters against
  `Scope.__members__.values()`), so a corrupted scope list fails closed.
- Every MCP tool handler calls `_require_scope(token, ...)` first
  (`server.py:212`): `search`/`get_document` need `search`; the graph
  tools (`get_entity`, `get_related_entities`, `list_entities`,
  `resolve_incident`, `blast_radius`) additionally call
  `_require_workspace`, which delegates to the fail-closed
  `get_workspace_id` — an unscoped token cannot touch the graph at all.

No findings.

## 3. Workspace / ACL enforcement

Reviewed: `packages/core/src/omniscience_core/auth/workspace.py`,
`packages/core/src/omniscience_core/auth/middleware.py`,
`packages/retrieval/src/omniscience_retrieval/graph_query.py`.

- `get_workspace_id` raises `PermissionError("forbidden:workspace_required")`
  on a `workspace_id`-less token — fail-closed, no "None means everything"
  semantics.
- `workspace_filter` applies strict equality on `workspace_id`/`tenant_id`
  and deliberately adds **no** `OR col IS NULL` clause; migration `0010`
  backfills legacy NULL rows to the default workspace.

**Finding F-1 (Medium) — NULL-tenant rows are workspace-visible in the SQL
graph traverser.** `_workspace_predicate` in
`packages/retrieval/src/omniscience_retrieval/graph_query.py` (line 142)
returns `or_(Source.tenant_id == workspace_id, Source.tenant_id == null())`.
This contradicts the strict-equality, no-NULL policy that
`workspace.py` adopted after migration `0010`: any source row whose
`tenant_id` is NULL (e.g. inserted by a path that bypasses the backfilled
schema) becomes visible to **every** workspace's graph traversals, seed
lookups included. Today the backfill should leave zero such rows, so this
is latent rather than exploitable, but it is a single-row-away regression.
*Fix:* drop the `Source.tenant_id == null()` disjunct and align with
`workspace_filter`; add a contract test that a NULL-tenant source is
invisible to all workspaces.

## 4. Hybrid retrieval path (dense + sparse BM25 + RRF)

Reviewed: `packages/index/src/omniscience_index/stores/qdrant_store.py`,
`packages/index/src/omniscience_index/stores/qdrant_filters.py`.

- All Qdrant filters are built through the frozen
  `QdrantFilterBuilder(workspace_id=...)`; its `build()` post-condition
  places `workspace_id` in the `must` clause unconditionally, and `with_*`
  narrowers can only tighten, never widen. `search` dispatch
  (`qdrant_store.py:936`) routes every strategy (`hybrid`, `keyword`,
  `structural`) through `_request_to_filter`, so no code path constructs a
  filter without the workspace clause.
- Filters are structured `qdrant_client.models` objects — there is no
  string query language on this path, hence no injection surface. The BM25
  side (`_tokenize_to_sparse`) hashes tokens client-side into a sparse
  vector; the user query never reaches a parser.

**Finding F-2 (Low) — `include_tombstoned` is caller-controlled on the MCP
search tool.** `apps/server/src/omniscience_server/mcp/server.py` (line
313) exposes `include_tombstoned: bool = False` to any `search`-scoped
token, and `_request_to_filter` (`qdrant_store.py:1163`) honours it. On the
direct hybrid path this lets a caller read tombstoned (deleted-but-not-yet-
purged) evidence **within their own workspace** until
`purge_tombstones`/`delete_tombstoned` runs. The GraphRAG path is not
affected (see §6). Not a cross-tenant issue, but it silently weakens the
deletion story. *Fix:* gate `include_tombstoned=True` behind `admin` scope
or remove it from the public tool schema.

## 5. tsvector/BM25 + SQL construction

Reviewed: `packages/index/src/omniscience_index/stores/postgres_only_store.py`,
`packages/core/src/omniscience_core/storage/vector.py`,
`packages/index/src/omniscience_index/stores/qdrant_store.py`.

- The lite store's `search` builds the ranking query with SQLAlchemy Core
  expressions (`Chunk.embedding.op("<=>")`) and bound parameters; the
  recursive graph traversal (`traverse`, line 445) uses `text()` with
  **bound** parameters only. The `{edge_types_clause}` placeholder is
  replaced with a fixed literal (`AND edge.edge_type = ANY(:edge_types)`),
  never with user input — no SQL injection surface found. Each recursion
  hop re-applies `s.tenant_id = :workspace_id`, so a planted cross-tenant
  edge is dropped rather than followed.
- `VectorStore.search` (`vector.py`) declares `workspace_id` keyword-only
  and required at the protocol level, so an adapter cannot be called
  unscoped by accident.

**Finding F-3 (Medium) — the lite store ignores `as_of`.**
`postgres_only_store.py` `search` (line 854) accepts `as_of` but never
applies the ADR-0008 §5 predicate — a time-travel query on a lite
deployment silently returns current-state data instead of the anchored
snapshot (or an error). This is a correctness/auditability gap in the
"cited, access-scoped evidence" contract rather than an access-control
hole (tenant scoping at line 885 is strict equality and unaffected).
*Fix:* either implement the `valid_from <= as_of < valid_to` predicate on
the lite schema or reject `as_of` requests with an explicit
`unsupported:as_of` error.

## 6. Cypher construction

Reviewed: `packages/index/src/omniscience_index/stores/neo4j/_cypher.py`,
`packages/index/src/omniscience_index/stores/neo4j/store.py`,
`packages/index/src/omniscience_index/stores/neo4j_store.py`.

- Every Cypher statement is a module-level `Final` template using `$param`
  placeholders; the f-string interpolation visible in `_cypher.py` injects
  only compile-time label/property constants (`_ENTITY_LABEL`, etc.),
  never request data. Entity names, workspace ids and timestamps all
  travel as driver parameters.
- Import-time regression guard `_ensure_workspace_predicate`
  (`_cypher.py:976`) refuses to load the adapter if any read/write template
  loses its `workspace_id` predicate — ACL isolation cannot silently rot.
- Writes derive `workspace_id` from the caller or the entity batch and
  fail with `upsert_graph_missing_workspace_id` when absent
  (`neo4j/store.py:272`); the legacy `neo4j_store.py` shim delegates to the
  same templates.

No findings.

## 7. as_of vs ACL/tombstone interaction

Reviewed: `packages/retrieval/src/omniscience_retrieval/graph_rag.py`,
`packages/core/src/omniscience_core/storage/graph.py`,
`packages/core/src/omniscience_core/storage/vector.py`,
`packages/index/src/omniscience_index/writer.py`,
`packages/index/src/omniscience_index/stores/qdrant_filters.py`.

The question round 1 could not answer: can a time-travel read
(`as_of=T` in the past) resurrect evidence that has since been tombstoned
or whose source's access was revoked? Answer: **no, on the reviewed
paths** — the tombstone/ACL filters are composed *with* the temporal
predicate, not replaced by it:

- In `_request_to_filter` (`qdrant_store.py:1131`), tombstone exclusion
  and the `as_of` predicate are independent `must` clauses from
  `QdrantFilterBuilder`: `exclude_tombstoned()` adds
  `tombstoned_at IS NULL` regardless of whether `with_as_of(T)` or
  `with_current_only()` is chosen. A chunk tombstoned at T2 is therefore
  invisible even at `as_of=T1 < T2`. Tombstoning is a **current-state
  security decision**, deliberately not bitemporal.
- The GraphRAG composer re-validates every merged hit against Postgres
  ground truth in `_validate_hits` (`graph_rag.py:1021`): tenant equality,
  `Source.status == active`, and `Document.tombstoned_at IS NULL` — with
  no `as_of` carve-out. `_validate_entities` applies the same policy to
  anchor-stage entity names. So even if a stale vector point or graph node
  survived, an ACL-revoked (deactivated source) or tombstoned document
  cannot be cited at any `as_of`.
- `writer.py` `tombstone` sets `Document.tombstoned_at` and forwards to
  the vector store's `delete_by_document`, which stamps the
  `tombstoned_at` payload on all of the document's points — keeping the
  Qdrant-side filter and the Postgres-side validator in agreement until
  `purge_tombstones` hard-deletes both.
- The protocol docs (`vector.py`, `graph.py`) codify this: `as_of` narrows
  to rows valid at T via the open-closed
  `valid_from <= as_of AND (valid_to > as_of OR valid_to IS NULL)`
  predicate (`qdrant_filters.py:219`), while workspace and tombstone
  clauses always lead.

Residual risks, tied to the findings above: the direct (non-GraphRAG)
hybrid path has no Postgres re-validation, so it relies solely on the
Qdrant payload flags — combined with F-2 this is what makes
`include_tombstoned` worth gating; and the lite store's ignored `as_of`
(F-3) means the lite path answers time-travel questions with current-state
data. Neither crosses a workspace boundary.

## Summary

| # | Severity | Surface | Location | Status |
|---|----------|---------|----------|--------|
| F-1 | Medium | workspace ACL (SQL graph traverser) | `packages/retrieval/src/omniscience_retrieval/graph_query.py:142` | open — remove NULL-tenant disjunct |
| F-2 | Low | tombstone visibility (MCP search) | `apps/server/src/omniscience_server/mcp/server.py:313` | open — gate behind admin scope |
| F-3 | Medium | as_of contract (lite store) | `packages/index/src/omniscience_index/stores/postgres_only_store.py:854` | open — implement or reject `as_of` |

Token handling, scope checks, workspace scoping on the Qdrant and Neo4j
paths, and all SQL/Cypher construction reviewed here are parameterised and
fail-closed. The "secure by design" convergence claim is now backed by
source, with the three findings above tracked as follow-ups.
