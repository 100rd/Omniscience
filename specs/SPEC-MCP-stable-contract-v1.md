# SPEC-MCP: Stable MCP Contract v1
Status: ready · Depends on: SPEC-EV, SPEC-KP, SPEC-ACL, SPEC-OPS
Readiness: human-approved by @100rd on 2026-07-15 under accepted ADR-0019

## Governing ADRs

- genai-enablement ADR-0017 (accepted) - pinned, freshness-aware, severable MCP v1
- Omniscience ADR-0019 (accepted) - human-ready task contracts and knowledge-plane boundary

## Goal

Publish one stable, content-addressed, workspace-scoped MCP contract that lets consumers prove wire
compatibility and evidence fitness before relying on Omniscience, while preserving direct-source
severance.

## Requirements

[REQ-MCP-1] The public contract version is `1.0.0`. Its packaged manifest binds the exact source Git
commit, every schema SHA-256, the complete tool-registry SHA-256, supported capabilities, and token
profile id. Release materialization reads those bytes from one clean exact non-zero Git `HEAD`, not the
mutable worktree, and runtime requires the resulting canonical manifest in addition to the matching
commit configuration. **Fallback:** an absent, malformed, mutable, env-only, non-canonical,
commit-mismatched, or digest-mismatched publication is unusable.

[REQ-MCP-2] The canonical registry contains exactly fifteen sorted tools: the fourteen existing wire
tools plus authenticated `contract_info`. Breaking wire removals, renames, type changes, or semantic
changes require a new major version. **Fallback:** registry skew selects direct-source fallback.

[REQ-MCP-3] `contract_info` requires a valid `omniscience-mcp-read-v1` token and returns the version,
source commit, manifest/schema/tool-registry digests, token-profile id, and supported capabilities.
**Fallback:** handshake absence or pin mismatch is contract mismatch, never implicit v0 negotiation.

[REQ-MCP-4] Every successful tool response preserves its existing top-level payload fields and adds a
schema-valid `meta` containing contract identity, token-derived workspace, generation/effective times,
freshness, consistency, and fallback. The producer serializes finite JSON against a fixed 80,000-byte
UTF-8 budget; non-JSON/non-finite or oversized output becomes a stable MCP error and is never returned
as a truncated schema-invalid success. **Fallback:** missing or invalid metadata makes the response
planning-only.

[REQ-MCP-5] Freshness uses only complete lineage for sources actually used by the answer. Its status is
one of `fresh`, `stale`, `unknown`, or `degraded`; missing lineage and never-synced sources cannot be
called fresh. Historical age is evaluated at `effective_as_of`; a source snapshot produced after that
boundary cannot prove historical lineage and is `unknown`. Proved ages are conservative integer
seconds. **Fallback:** stale or insufficiently known evidence requires direct authoritative source queries.

[REQ-MCP-6] Consistency distinguishes Postgres authority from Neo4j/Qdrant projections and reports
version distance as `projection_lag_versions`. Legacy `staleness_seconds` may remain temporarily but
cannot be interpreted as elapsed projection lag or copied into the v1 field. Only explicit non-negative
integer version evidence is admitted. **Fallback:** divergent or unavailable used stores set degraded
consistency and require fallback.

[REQ-MCP-7] `fallback.required` is true for relevant stale sources, missing lineage, store divergence,
unusable freshness/consistency, or contract pin mismatch. The reason is a stable schema enum/string.
**Fallback:** a consumer may be stricter but cannot override `required=true` through LLM judgment.

[REQ-MCP-8] The bootstrap profile is exactly `omniscience-mcp-read-v1`: issued by the Omniscience admin
token API, mandatory server-bound `workspace_id`, exactly scope `search`, mandatory expiry no later than
30 days, and no legacy/extra-scope acceptance. **Fallback:** invalid profile tokens are rejected before
tool execution without cross-workspace disclosure.

[REQ-MCP-9] Token creation, rotation, and revocation are audited. Rotation overlaps the predecessor for
at most 24 hours without extending either expiry. A predecessor has at most one successor: rotation
locks the predecessor and a unique lineage constraint rejects repeated or racing replacement attempts.
Revocation terminates acceptance independently.
**Fallback:** missing audit/lifecycle enforcement blocks consumer activation.

[REQ-MCP-10] V0 documentation is explicitly superseded and v1 is the stable public contract. Runtime
registry, manifest, schemas, docs, SPEC indexes, roadmap, execution evidence, and `.mcp` catalogs pass
one deterministic offline drift check. The Python MCP SDK remains on the verified stable v1 line
(`mcp>=1.27,<2`) until an explicit v2 contract migration. **Fallback:** skew fails CI.

[REQ-MCP-11] Omnius and SRE consumers recognize stale, unknown, degraded, and digest-mismatch states and
switch to direct authoritative sources. Already materialized work survives Omniscience loss.
**Fallback:** if direct access also fails, park rather than invent context.

[REQ-MCP-12] Canary rollout precedes consumer pinning; a live severance drill and human-reviewed
execution evidence precede terminal task state. **Fallback:** passing unit tests alone does not activate
the integration or certify production HA.

## Interfaces

```text
McpContractV1.manifest -> content-addressed manifest + schemas + tool registry
contract_info(authenticated workspace token) -> immutable pin + supported capabilities
McpSuccessV1 = LegacySuccessfulPayload + { meta: ContractMetaV1 }
Consumer.accept(response) -> use | direct_source_fallback | park
```

## Verification

- P-MCP-1 recomputes every manifest/schema/registry digest, rejects one-byte drift, refuses dirty or
  non-HEAD materialization, and proves release runtime rejects env-only or tampered provenance.
- P-MCP-2 compares the FastMCP AST registry, packaged registry, docs, and all catalogs to the same
  canonical fifteen names.
- P-MCP-3 accepts the exact handshake pin and rejects version, commit, schema, and registry mismatch.
- P-MCP-4 validates `meta` for every successful tool, proves legacy top-level fields unchanged, and
  rejects non-finite/non-JSON or oversized output as a tool error rather than invalid success.
- P-MCP-5 covers fresh, stale, never-synced, mixed-source, missing-lineage, and explicit-`as_of` cases,
  including post-boundary snapshots and integer age compatibility with pinned consumers.
- P-MCP-6 injects Neo4j/Qdrant lag/divergence, verifies explicit integer version metadata, and proves
  legacy `staleness_seconds` never becomes `projection_lag_versions`.
- P-MCP-7 proves every mandatory degradation sets `fallback.required=true` with a stable reason.
- P-MCP-8 proves token-derived workspace, foreign/nonexistent non-disclosure, exact scope, expiry, and
  maximum lifetime.
- P-MCP-9 verifies bounded single-successor rotation, concurrent-lineage constraint, revoke, and
  create/rotate/revoke audit records.
- P-MCP-10 mutates each documentation/manifest/index/catalog surface or the stable SDK-major pin and
  observes offline CI failure.
- P-MCP-11 removes/mismatches Omniscience and proves omnius/SRE direct-source continuation or safe park.
- P-MCP-12 records canary, consumer pin, live severance drill, and human verification in the execution
  index before terminal state.
