# SPEC-KP: Dark Factory Knowledge Plane
Status: ready · Depends on: SPEC-EV, SPEC-ACL, SPEC-OPS
Readiness: human-approved by @100rd on 2026-07-11 under accepted ADR-0019

## Governing ADRs

- Omniscience ADR-0015 - MCP consumer/source integration without Experience ownership
- Omniscience ADR-0019 (accepted) - read-only and severable Dark Factory boundary

## Goal

Supply grounded planning and operational evidence without becoming the factory's correctness oracle,
policy engine, state owner, or action executor.

## Requirements

[REQ-KP-1] Dark Factory access uses the standard workspace-scoped MCP retrieval surface; no hidden
consumer-specific database or admin path. **Fallback:** unavailable MCP routes to direct authoritative
sources, not an unscoped REST shortcut.

[REQ-KP-2] Omniscience may provide context, citations, topology, blast radius, replay, and incident
returns. It cannot decide task readiness, correctness, merge, autonomy, apply, or promotion. **Fallback:**
unknown tool category is denied from the factory integration.

[REQ-KP-3] A downstream deterministic gate cannot use Omniscience confidence or synthesis as its sole
oracle. Ground truth comes from the target repo/runtime/incident system. **Fallback:** no external oracle
means human review, never GREEN.

[REQ-KP-4] Consumers pin evidence ids, revisions, effective `as_of`, and query contract into task
evidence. **Fallback:** an unpinnable/mutable response is planning-only and cannot support a gate.

[REQ-KP-5] Active tasks survive knowledge-plane loss after materialization. Planning falls back to direct
git/Kubernetes/cloud/incident sources and records degraded mode. **Fallback:** if direct-source access is
also unavailable, park rather than invent context.

[REQ-KP-6] Omniscience ingests facts about agent/factory runs but never subjective lessons, autonomy
policy, or Experience confidence. **Fallback:** Experience-shaped writes are rejected or routed to the
owning factory plane.

[REQ-KP-7] Insight Mode remains read-only against customer infrastructure. Deterministic postmortem
rendering is a review artifact, not remediation. **Fallback:** any new synthesis/action tool requires a
Full SDD ADR and stays disabled until accepted and probed.

[REQ-KP-8] Priority or consumer identity cannot bypass freshness, tenant, epoch, citation, or degradation
rules. **Fallback:** unknown evidence fitness returns explicit degraded/unavailable state.

## Interfaces

```text
KnowledgePlane.search(workspace, query, as_of, filters) -> EvidenceEnvelope
KnowledgePlane.replay(workspace, subject, as_of) -> EvidenceEnvelope
KnowledgePlane.severance_snapshot(envelope) -> PinnedEvidenceManifest
```

## Verification

- P-KP-1 proves the factory token cannot access admin/direct-store paths.
- P-KP-2 rejects readiness/merge/apply tools from the MCP registry.
- P-KP-3 attempts confidence-only GREEN and deterministically routes to human/external oracle.
- P-KP-4 replays a pinned manifest after head/projection changes with stable provenance.
- P-KP-5 disables Omniscience mid-task and completes from pinned/direct-source context or parks safely.
- P-KP-6 rejects Experience/lesson writes while accepting factual Outcome/Return events as sources.
- P-KP-7 arch-test permits retrieval and governed deterministic synthesis, never infrastructure action.
- P-KP-8 verifies privileged consumer tokens receive the same evidence-fitness enforcement.
