# SPEC-PII: PII Wall — ingestion, knowledge stores, and lifecycle

Status: ready · Depends on: SPEC-ACL, SPEC-SOT, SPEC-EV, SPEC-OPS · signal-only: SPEC-KP, SPEC-MCP

## Governing ADRs

- genai-enablement ADR-0018 (accepted) - distributed, purpose-bound PII Wall
- ADR-0020 (accepted) - Omniscience PII Wall adoption and development boundary
- ADR-0019 (accepted) - governed evidence boundary and capability readiness

## Goal

Prevent unadmitted personal data from propagating through Omniscience. Classification and policy
admission happen before durable raw-document storage, parsing, chunking, embedding, graph/vector
projection, archive, retrieval, or external egress; lifecycle operations return exact coverage
receipts across every knowledge-store class.

## Scope

**In:** server-derived tenant/workspace and source policy; pre-ingest classification; quarantine;
redaction/tokenization adapters; PW0/PW1/PW2 admission; storage and projection labels; embedding-provider
admission; retrieval revalidation; correction/export/deletion/retention coverage; non-identifying
privacy evidence.

**Out:** legal classification and consent; policy authorship; Omnius prompt/model/tool enforcement;
portal rendering; a general token-reidentification service; changing current ingestion runtime through
this draft.

## Requirements

[REQ-PII-1] **Scope and policy are server-derived before content handling.** The authenticated principal,
Source mapping, and signed policy revision determine tenant, workspace, source class, and allowed
profile. Connector payloads, document content, metadata, and caller labels cannot widen them.
**Fallback:** missing, foreign, ambiguous, expired, or signature-invalid scope/policy rejects before
decode and emits only a scoped failure receipt.

[REQ-PII-2] **Classification precedes the first durable or external sink.** Structured schema rules and
deterministic detectors run before a raw document, parsed structure, chunk, embedding request, vector,
graph node, cache, outbox payload, archive, backup seed, or provider request is created. Statistical/LLM
classification may widen but never downgrade. **Fallback:** detector error, incomplete coverage, or
unknown classification quarantines or blocks; the ordinary ingestion pipeline receives no content.

[REQ-PII-3] **PW0 is the default admission profile.** Only `public`, `internal_non_personal`, or
deterministically redacted content may enter normal stores, projections, embeddings, or retrieval under
PW0. Raw `personal`, `sensitive_personal`, `prohibited`, and `unknown` data is rejected or held in an
owner-controlled sealed quarantine outside normal indexes. **Fallback:** if the quarantine boundary is
not independently qualified, reject without retaining the body.

[REQ-PII-4] **Every transformation is deterministic, field-aware, and receipted.** Redaction or
pseudonymization records input digest, optional safe-output digest, policy/detector revisions,
field-level transformation set, coverage, and disposition in a `SanitizationReceipt`. Receipts contain
no raw PII. **Fallback:** partial transformation or unrecognized structure remains quarantined and
cannot be treated as sanitized.

[REQ-PII-5] **PW1 pseudonyms are non-global and non-reidentifying here.** Tokens are tenant-scoped and,
when correlation is needed, purpose/source scoped. Omniscience does not hold a general reveal key or
expose a reveal tool. Raw and tokenized values cannot be searched together to recreate identity.
**Fallback:** unavailable or over-broad tokenization falls back to irreversible redaction or PW0 reject.

[REQ-PII-6] **PW2 requires an exact external permit.** Raw processing requires a valid `PIIPermit`
binding source, selected fields, processing purpose, Omniscience sink/store/provider set, workload,
tenant/workspace, retention deadline, policy revision, and expiry. Field, sink, provider, purpose, or
time widening denies. **Fallback:** absent/expired/unverifiable permit blocks before the selected sink;
there is no administrator or model best-effort override in the ingestion path.

[REQ-PII-7] **Classification and lineage propagate to every derivative.** Document, parsed object, chunk,
embedding/vector, graph entity/edge, cache, outbox, archive, and backup manifest retain the envelope id,
class, transformation state, policy revision, source lineage, purpose/permit reference where applicable,
and retention deadline. **Fallback:** a store adapter that cannot persist and enforce the minimum
metadata is not admitted for PW1/PW2 and receives PW0-safe data only when independently proven.

[REQ-PII-8] **Retrieval and egress re-evaluate the consumer sink.** Search, MCP/API response, export,
connector callback, synthesis, and embedding-provider calls validate workspace, profile, purpose,
allowed fields, policy freshness, and requested sink. Retrieval never silently rehydrates redacted
data or returns a pseudonym outside its correlation scope. **Fallback:** mismatch produces a
non-disclosing deny or a PW0-safe projection with an explicit degraded/coverage state.

[REQ-PII-9] **Lifecycle operations cover ledger, projections, caches, archives, and backups.** Correction,
export, retention expiry, and deletion enumerate Postgres, Neo4j, Qdrant, object/archive storage,
caches, outboxes, checkpoints, quarantine, and backup generations. A `DeletionReceipt` names completed,
excluded, pending, unavailable, and immutable-retention classes. **Fallback:** partial work parks and
alerts; no API, task, or UI may report complete.

[REQ-PII-10] **Privacy observability is useful without becoming a leak.** Metrics expose counts, latency,
coverage, policy/detector revisions, store classes, and stable pseudonymous references. Logs, traces,
errors, receipts, evidence bundles, and operational UI contain no raw values or reversible fingerprints.
**Fallback:** an unsafe diagnostic field is dropped and the event records `redacted_diagnostic`; loss of
the privacy evidence channel blocks readiness claims but not fail-closed enforcement.

## Interfaces

**Exposes:**

```text
IngestPIIGate.evaluate(source, principal, body_ref, policy_pin) -> admitted envelope | quarantine | deny
KnowledgePIIProjection.read(scope, purpose, requested_fields) -> safe DataEnvelope[]
PrivacyCoverage.read(workspace_id, policy_revision) -> PrivacyCoverage
LifecyclePII.execute(request) -> DeletionReceipt | partial | denied
```

**Consumes:** signed `PIIPolicyBundle` and `PIIPermit` contracts; SPEC-ACL workspace resolution;
SPEC-SOT store/outbox semantics; SPEC-EV lineage; SPEC-OPS evidence discipline. SPEC-KP and SPEC-MCP
consume only released safe projections and coverage metadata.

## Verification

- **P-PII-1 pre-propagation:** seed each class and prove disallowed bytes never reach raw documents,
  chunks, embedding requests, Postgres, Neo4j, Qdrant, cache, outbox, archive, or logs.
- **P-PII-2 fail-closed matrix:** missing/expired/skewed policy, detector exception, forged workspace,
  unknown class, permit expiry, field/sink/provider widening, and quarantine outage all reject before
  ordinary ingestion.
- **P-PII-3 transformation:** structured and free-text fixtures produce expected field manifests and
  receipts; partial parsing never claims sanitized.
- **P-PII-4 pseudonym isolation:** equal inputs in two tenants/purposes do not produce a joinable global
  identity, and no reveal surface is reachable.
- **P-PII-5 retrieval:** PW0/PW1/PW2 consumers receive only their admitted fields; MCP/search/export
  cannot rehydrate or bypass purpose.
- **P-PII-6 lifecycle:** delete one seeded subject/workspace across every store, restore an older backup,
  reapply deletion, and prove the receipt remains partial until all declared coverage closes.
- **P-PII-7 telemetry:** scan response/log/trace/metric/evidence/UI corpora for every seeded raw value and
  reversible token; results are zero.
- **P-PII-8 severance:** policy or evidence-plane loss blocks new propagation while already sanitized,
  permitted reads follow explicit freshness/expiry rules.

## Invariants honored

ADR-0018 PII-1 through PII-8 and PII-10; SPEC-ACL server-derived workspace and non-disclosure; SPEC-SOT
ledger/projection convergence; SPEC-EV lineage; SPEC-KP/MCP severability.

## Open questions / deferred

- [resolved-for-development] Consume the exact content-addressed `SPEC-PII-POLICY` bundle and a
  deployment-supplied signer trust profile. Development uses a fixture signer; no live trust root or
  active policy is defaulted.
- [resolved-for-development] Implement the ADR-0020 `QuarantineStore` interface with disposable
  fixtures. A live backend, access path, encryption, maximum retention and purge/forensic authority are
  required before live activation.
- [resolved-for-development] Treat every embedding/enrichment provider as an explicit policy sink and
  deny when its exact field, retention, training and region posture is absent. No protected-data provider
  is enabled by this SPEC.
- [must-resolve-before-PW1] Tokenization algorithm, scope derivation, collision policy, and separate
  re-identification-vault owner if reversible tokens are admitted.
- [must-resolve-before-PW2] Permit issuer, purpose registry, field grammar, emergency process, and
  subject/lifecycle correlation contract.
