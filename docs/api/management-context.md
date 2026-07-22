# Management context and knowledge-quality producer (SPEC-MCTX, task-sp-81)

**Status:** development / non-live. Read-only producer code, schemas, and fixtures only -- no
live consumer pin, production token, provider/model call, or Barbarossa activation. See
[ADR-0021](../decisions/0021-management-context-producer.md) and
[`contracts/management/README.md`](../../contracts/management/README.md).

## What this is

Omniscience publishes a versioned `ManagementContextBundle` and `KnowledgeQualitySnapshot`
producer for Barbarossa (genai-enablement's Continuous Management Plane, ADR-0020) and other
management consumers. It returns cited, scoped, severable knowledge context -- never a domain
outcome, verdict, risk acceptance, action recommendation, approval, or verification.

## Interfaces

```text
ManagementContextRequest              -- tenant/workspace, subject, domain, purpose,
                                          requested field classes, evidence cut, max age,
                                          contract/schema revisions, caller identity
ManagementContextBundle               -- request digest, producer/source revisions,
                                          citations, coverage gaps, conflict set,
                                          projection state, PII receipt, quality, integrity
ManagementCitation                    -- source ref, uri, snapshot digest, evidence fitness
KnowledgeQualitySnapshot              -- provenance/freshness/conformance/coverage/conflict/
                                          projection axes, reported independently, no score
ManagementContextCapabilityManifest   -- contract major, schema revisions, producer digest
```

Python implementation: `packages/core/src/omniscience_core/management/` (producer, taxonomy,
scope validation, PW0-aware field admission, authority/PII conformance scanner, contract-skew
and severance handling). Server boundary: `apps/server/src/omniscience_server/management/`.
Schemas: `contracts/management/schemas/v1/`.

## Producer outcomes

`ManagementContextProducer.produce(...)` returns exactly one of:

| Outcome | When | Notes |
|---|---|---|
| `.bundle` | request in scope, contract pinned, evidence source healthy | schema-valid, conformance-clean |
| `.denial` | scope rejected before retrieval (foreign workspace, unknown domain/purpose, widened field class, future `evidence_cut`, invalid `max_age`, malformed payload) | evidence source is never touched |
| `.fallback` | contract skew (major/schema/manifest mismatch) or severance (producer/source unavailable, stale evidence, authority-field leak, invalid bundle schema) | `fallback_required=true` with a closed-enum `reason` |

## Field admission

Every candidate citation field passes two independent gates before it can appear in a bundle:

1. **PW0 admission** (`omniscience_core.privacy.taxonomy.is_pw0_admitted`) -- the same predicate
   that gates ordinary ingestion. A non-admitted field is dropped and recorded as a coverage gap,
   never silently included.
2. **Authority/PII conformance scan** (`omniscience_core.management.conformance`) -- forbidden
   authority-truth field names, credential-shaped tokens, active-content markers, and seeded-PII
   value shapes. A violation drops the citation (coverage gap) or, if detected on the assembled
   bundle itself, forces `fallback_required=authority_field_detected` instead of ever returning
   the bundle.

## Non-activation boundary

- No live consumer pin, production token, provider/model call, Barbarossa decision, deployment,
  or production-readiness claim (ADR-0021 development authority).
- No global quality score, verdict, or management truth field exists anywhere in the schema.
- Every fixture used by the test suite is disposable and synthetic
  (`tests/management/fixtures.py`); none is a live evidence source or real personal data.

## Running the probes

```bash
python3 -m pytest tests/test_management_context_scope_and_replay.py \
  tests/test_management_context_authority_and_pii.py \
  tests/test_management_context_severance.py \
  tests/test_management_context_boundary.py tests/management -v
```
