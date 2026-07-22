# SPEC-MCTX: Management context and knowledge-quality producer

Status: ready · Depends on: SPEC-ACL, SPEC-EV, SPEC-KP, SPEC-MCP, SPEC-OPS · signal-only: SPEC-PII

## Governing ADRs

- genai-enablement ADR-0017 and ADR-0020
- Omniscience ADR-0019 and ADR-0021

## Goal

Publish cited, scoped and severable context for Barbarossa and other management consumers without
turning knowledge retrieval, synthesis or quality scoring into management truth or authority.

## Scope

**In:** request authorization; exact management purpose; evidence fitness; citations; provenance,
freshness, coverage, conflicts and projection health; knowledge-quality snapshots; PII-safe field
selection; version negotiation; content-addressed fixtures and severance behavior.

**Out:** domain observations or snapshots; SLO/risk/budget policy; incident/action decisions; arbitrary
graph access; agent/model recommendations presented as facts; consumer implementation; live activation.

## Requirements

[REQ-MCTX-1] A request binds tenant/workspace, subject, management domain and purpose, requested field
classes, source/evidence cut, maximum age, contract/schema revisions, caller identity and correlation id.
**Fallback:** missing, foreign, widened, future or unsupported scope is denied without retrieval.

[REQ-MCTX-2] A `ManagementContextBundle` binds the request digest, producer/source revisions, produced
and observed times, freshness deadline, coverage and source-health axes, citations, conflict set,
projection state, PII receipt and integrity. **Fallback:** an incomplete bundle is unavailable, never a
partial favorable result.

[REQ-MCTX-3] Every factual statement is joined to one or more stable source citations and exact evidence
fitness. Generated summaries are explicitly typed `synthesis` and cannot create an uncited fact.
**Fallback:** unsupported content is omitted and recorded as a coverage gap.

[REQ-MCTX-4] `KnowledgeQualitySnapshot` reports provenance, freshness, conformance, coverage, conflict
and projection conditions independently. It publishes no global quality score and no management verdict.
**Fallback:** missing required evidence yields `unknown`, `partial` or `severed` on the affected axis.

[REQ-MCTX-5] The producer never emits availability, error-budget, incident, cost opportunity, risk,
compliance, action, approval, effect or verification truth. **Fallback:** forbidden authority fields fail
schema and conformance checks.

[REQ-MCTX-6] PII policy is applied before retrieval projection and response assembly. The output carries
only admitted fields and non-identifying receipts; active content, credentials and raw personal data are
forbidden. **Fallback:** use a narrower safe projection or return privacy-blocked.

[REQ-MCTX-7] Consumers pin an exact contract major, schema and producer manifest. Unknown major, digest
skew or incompatible capability returns typed fallback-required. **Fallback:** no best-effort parsing.

[REQ-MCTX-8] Severance is first-class. Producer loss, token revocation, source outage and stale evidence
return distinguishable states; already materialized consumer work continues from direct sources or
parks under consumer policy. **Fallback:** no cached context is represented as current beyond expiry.

[REQ-MCTX-9] Access is read-only, least-privilege and purpose scoped. The API exposes closed query
profiles, not arbitrary Cypher/vector queries or browsing of another workspace. **Fallback:** unsupported
query shapes are denied and audited without echoing protected values.

[REQ-MCTX-10] Context artifacts are content-addressed and replayable against fixtures. Same inputs and
evidence cut produce the same envelope metadata and citation set; optional synthesis cannot affect the
deterministic quality snapshot. **Fallback:** nondeterministic synthesis is excluded from authority and
replay comparisons.

## Interfaces

```text
ManagementContextRequest
ManagementContextBundle
ManagementCitation
KnowledgeQualitySnapshot
ManagementContextCapabilityManifest
```

## Verification

- **P-MCTX-1 scope:** cross-tenant/workspace/purpose/field widening is denied before retrieval.
- **P-MCTX-2 citations:** every fact has a valid citation and missing citations become coverage gaps.
- **P-MCTX-3 no-authority:** forbidden management verdict/action fields fail static and schema checks.
- **P-MCTX-4 quality axes:** missing/conflicting evidence cannot collapse to a favorable scalar.
- **P-MCTX-5 PII/content:** seeded PII, credentials and active content never reach bundle or logs.
- **P-MCTX-6 skew/severance:** version mismatch, token revocation and producer/source loss select exact
  fallback-required states without false freshness.
- **P-MCTX-7 replay:** deterministic envelope/citation/quality outputs reproduce from pinned fixtures.

## Open questions / deferred

- [must-resolve-before-live] Production identity issuer, token TTL, endpoint and rate/admission profile.
- [must-resolve-before-live] First released consumer revision and live severance receipt.
- [defer] Optional model-generated narrative after separate provider/PII admission; deterministic fields
  remain complete without it.
