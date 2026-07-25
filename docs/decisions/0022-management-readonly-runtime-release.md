# ADR-0022: Adopt the Management Read-Only runtime release boundary

Status: accepted
- **Date:** 2026-07-25
- **Deciders:** platform owner and Omniscience owner
- **Governing decisions:** `genai-enablement` ADR-0012, ADR-0017, ADR-0018, ADR-0020, ADR-0021 and ADR-0022
- **Related local decisions:** ADR-0019, ADR-0020 and ADR-0021

## Context

The first synchronized-platform deployment profile selects Omniscience, Barbarossa and Platform Portal
while deferring the Omnius Dark Factory. Omniscience already has repository-local MCP v1, PW0 and
management-context implementations, plus container and Helm packaging. Those facts do not yet form one
immutable runtime release or prove that the selected consumers can use and sever it safely.

The release must not make Omniscience a Reliability dependency or management authority. It must also
avoid requiring an Omnius consumer receipt for a profile in which Omnius is deliberately not deployed.
The newly explicit Barbarossa Go decision must not leak a consumer implementation language into the
Omniscience producer contract or serialize SP-86 behind the independent SP-90 migration.

## Decision

Omniscience adopts `management-readonly-v1` through one owner-produced release contract:
`OmniscienceManagementReadOnlyRelease`.

The release binds the exact Git commit, image digest, Helm chart/config digest, MCP and management-context
contract manifests, schema digests, token/auth profile, SP-60 policy revision, SP-61 PW0 evidence,
dependency/readiness profile, supported consumer class, evidence references, and rollback target.
Mutable tags, branches, `latest`, dirty trees and unpinned policy/configuration cannot enter the release.

Producer schemas, fixtures and verification material are language-neutral. A Go consumer can validate
and consume the release without executing Node.js or depending on a TypeScript-only generated binding.
SP-86 has no dependency on the Barbarossa SP-90 migration and may execute concurrently with it. The
later SP-87 consumer task, not Omniscience, joins exact SP-86 and SP-90 receipts.

The selected runtime surface is read-only for consumers. It exposes MCP v1 evidence/context reads,
management context and knowledge-quality projections, non-identifying privacy coverage/receipts, and
owner-local health/readiness/metrics. It exposes no management verdict, action recommendation,
authorization, owner mutation, raw-PII permit or generic query surface.

PW0 remains fail closed before raw persistence, parsing, chunking, embedding, graph/vector projection,
provider egress, retrieval response, telemetry, archive and backup. A seeded-PII failure cannot be
masked by sanitizing only the response.

Barbarossa and Platform Portal consume the release under their own tasks and return their own exact
contract/severance receipts. Omniscience may validate supplied receipts read-only but cannot create,
repair or sign on behalf of a consumer. Omnius is not a selected consumer and its absence is not a
producer error for this profile. The existing full SP-12 Omnius/SRE conformance task remains valid and
separate.

Omniscience loss removes optional Barbarossa context and its Portal panels. Barbarossa continues from
direct authoritative observations or parks only when those sources also fail. No cache, summary or
prior success is promoted to current truth after freshness or contract expiry.

## Release invariants

- **OMR-1:** every executable, schema, policy and configuration input is content addressed.
- **OMR-2:** consumer reads are tenant/workspace and purpose bound with a closed field set.
- **OMR-3:** PW0 failure precedes every durable, derived, provider and export boundary.
- **OMR-4:** context never contains a domain outcome, incident decision, priority, approval or effect.
- **OMR-5:** readiness reports the selected dependency closure and cannot call a disabled worker healthy.
- **OMR-6:** consumer severance is typed and retains no stale-current success.
- **OMR-7:** Omnius endpoint, credential, mock and receipt are absent from the release configuration.
- **OMR-8:** the release receipt grants no deployment activation or management authority.
- **OMR-9:** the producer kit has no TypeScript/Node-only consumer requirement and SP-86 remains
  independent of SP-90.

## Development authority

This decision authorizes the bounded SP-86 implementation and disposable qualification in
`task-sp-86-management-readonly-release`. It does not authorize production data, raw PII, a model or
external provider, a production credential, an infrastructure mutation, deployment activation,
Barbarossa truth, Portal truth, Omnius work, or a production-readiness claim.
