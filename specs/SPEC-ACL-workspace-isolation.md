# SPEC-ACL: Workspace & Tenant Isolation
Status: draft · Depends on: SPEC-SOT

## Governing ADRs

- ADR-0019 (proposed) - governed evidence boundary
- hardening review 2026-06-10 - fail-closed workspace isolation evidence

## Goal

Make workspace identity server-derived, non-forgeable, and consistently enforced across ingestion,
ledger, projections, retrieval, synthesis, operations, and connectors.

## Requirements

[REQ-ACL-1] Workspace comes from the authenticated token/principal and server-owned Source mapping,
never event payload, query prompt, connector document, or agent claim. **Fallback:** missing/ambiguous
mapping rejects with no work.

[REQ-ACL-2] Source creation always writes tenant/workspace from the authenticated principal; NULL and
caller-supplied foreign ids are impossible. **Fallback:** unresolved token workspace rejects creation.

[REQ-ACL-3] Every Postgres/Neo4j/Qdrant read and write includes exact workspace scope. Legacy/null tenant
rows are invisible until explicitly migrated. **Fallback:** missing scope fails before store access.

[REQ-ACL-4] Cross-tenant object existence is not disclosed: foreign and nonexistent ids return the same
contract response and no timing/detail distinction usable as an oracle. **Fallback:** generic not-found.

[REQ-ACL-5] Connector secrets and `secrets_ref` are workspace-scoped and never returned by retrieval,
logs, traces, or error bodies. **Fallback:** unresolved/foreign secret reference blocks ingestion.

[REQ-ACL-6] Cache, reconciliation, checkpoint, outbox partition, benchmark fixture, and DR restore keys
include workspace. **Fallback:** unscoped shared state is disabled in multi-tenant mode.

[REQ-ACL-7] Admin and synthesis surfaces require explicit scopes in addition to workspace; `search`
scope cannot escalate. **Fallback:** unknown tool/scope denies.

[REQ-ACL-8] Workspace deletion/export/retention applies across ledger and projections with independently
verified completion. **Fallback:** partial operation parks and alerts without claiming completion.

## Interfaces

```text
WorkspaceResolver.from_token(principal) -> workspace_id
SourceWorkspace.resolve(source_id, principal) -> workspace_id | denied
ScopedStore.operation(workspace_id, ...) -> result
```

## Verification

- P-ACL-1 forges workspace in every external payload and observes server-derived scope.
- P-ACL-2 creates a source and proves non-NULL token workspace plus foreign-id rejection.
- P-ACL-3 runs cross-store matrix tests for missing/foreign workspace reads and writes.
- P-ACL-4 compares foreign/nonexistent API/MCP responses for non-disclosure.
- P-ACL-5 scans response/log/trace corpus for seeded connector secrets.
- P-ACL-6 injects identical ids across workspaces and proves cache/outbox/checkpoint isolation.
- P-ACL-7 exercises least-privilege scope matrix for retrieval/admin/synthesis.
- P-ACL-8 deletes/exports one workspace while another remains byte-for-byte accessible.

