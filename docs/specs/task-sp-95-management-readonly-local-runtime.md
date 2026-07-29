---
id: task-sp-95-management-readonly-local-runtime
title: Publish the Omniscience local owner fragment for Management Read-Only
status: ready
readiness:
  approvedBy: "@100rd"
  approvedAt: 2026-07-29
source: { kind: human, ref: "SP-95" }
governingAdrs: [genai-enablement/ADR-0018, genai-enablement/ADR-0021, genai-enablement/ADR-0024, Omniscience/ADR-0020, Omniscience/ADR-0021, Omniscience/ADR-0022, Omniscience/ADR-0023]
capabilitySpecs: [SPEC-IN, SPEC-SOT, SPEC-ACL, SPEC-EV, SPEC-OPS, SPEC-KP, SPEC-MCP, SPEC-PII, SPEC-MCTX]
sddMode: full
repo: 100rd/Omniscience
executionProfile: management-readonly-local-v1-omniscience-owner
evidenceDestination: ci-artifact://synchronized-platform/task-sp-95-management-readonly-local-runtime/
scope:
  include:
    - Dockerfile
    - .dockerignore
    - deploy/compose/management-readonly-local/**
    - apps/server/src/omniscience_server/app.py
    - apps/server/src/omniscience_server/routes/health.py
    - apps/server/src/omniscience_server/management/**
    - apps/server/src/omniscience_server/privacy/**
    - packages/core/src/omniscience_core/management/**
    - packages/core/src/omniscience_core/privacy/**
    - contracts/releases/management-readonly-local-v1/**
    - scripts/qualify_management_readonly_local.py
    - tests/release/management_readonly_local/**
    - tests/test_management_readonly_local*.py
    - docs/runbooks/management-readonly-local.md
    - .github/workflows/management-readonly-local.yml
  exclude: [docs/decisions/**, docs/specs/**, specs/**, graphify-out/**, infra/terraform/**]
acceptanceCriteria:
  - id: AC-SP95-1
    requirement: One owner-contained namespaced Compose fragment runs independently in image and source modes
    probe: omniscience-local-fragment-render
    expected: exact api admin postgres nats neo4j qdrant services plus owner networks volumes health and source override render without generic resource names host-port collisions mutable image tags or parent-owned migrations
    groundTruth: Docker Compose canonical model exact SP-86 image/source identities and independently enumerated resources
  - id: AC-SP95-2
    requirement: Startup and readiness cover the complete selected dependency and migration closure
    probe: omniscience-local-startup-readiness
    expected: bounded health-gated start succeeds and each induced PostgreSQL NATS Neo4j Qdrant migration or policy-init failure keeps the API unready with a typed non-identifying reason
    groundTruth: direct dependency probes migration records service health timeline and API readiness responses
  - id: AC-SP95-3
    requirement: The local service contract exposes only exact tenant workspace purpose and read-only Management Read-Only surfaces
    probe: omniscience-local-consumer-contract
    expected: exact MCP context knowledge-quality privacy and health reads pass while foreign stale skewed generic-query owner-write action effect and unregistered consumer requests fail closed
    groundTruth: exact SP-86 contract release local service-contract schema independently minted identities and positive/negative API captures
  - id: AC-SP95-4
    requirement: PW0 and local embeddings protect every bootstrap and runtime sink without an external provider
    probe: omniscience-local-pw0-provider-severance
    expected: safe synthetic fixtures become retrievable while seeded PII reversible tokens and active content reach no store graph vector embedding response log trace metric archive or backup and no provider network call occurs
    groundTruth: independent fixture corpus store and egress captures pinned local model identity SP-60 policy and SP-61 lifecycle evidence
  - id: AC-SP95-5
    requirement: Normal restart preserves admitted owner state and severance remains explicit
    probe: omniscience-local-persistence-severance
    expected: stop start and API restart preserve safe fixture lineage and converge projections while dependency or owner loss becomes typed unavailable and never cached-current healthy or empty
    groundTruth: named-volume identities before-after owner reads dependency lifecycle timeline and consumer captures
  - id: AC-SP95-6
    requirement: The fragment remains laptop-bounded and exposes only approved loopback UI diagnostics
    probe: omniscience-local-resource-exposure
    expected: amd64 and arm64 measurements fit the assigned share of the platform reference envelope and socket inventory contains no public API datastore broker graph or vector-store binding
    groundTruth: named host Docker version resource trace image sizes and host/container socket inventory
  - id: AC-SP95-7
    requirement: One immutable local owner receipt binds artifacts configuration behavior and non-authority fields
    probe: omniscience-local-runtime-receipt
    expected: Git or image service-contract rendered-fragment model policy fixture health PW0 persistence resource and rollback digests re-derive while dirty source forces reproducible false and availability_class ha_qualified activation_authority remain development-single-host false none
    groundTruth: clean or explicitly dirty source identity OCI registry where applicable evidence manifest and post-rollback capture
rollback: { kind: disposable, probe: stop-omniscience-local-fragment-preserve-volumes-and-restore-exact-prior-sp86-artifacts }
---

## Intent

Package the exact SP-86 Omniscience release as an independently runnable owner fragment for
`management-readonly-local-v1`. The task may harden local packaging, readiness and fixtures only inside
the listed paths. It cannot modify the parent composition or a consumer.

## Required inputs

- exact SP-86 `OmniscienceManagementReadOnlyRelease` and SP-60/SP-61 policy evidence;
- pinned local embedding model/cache identity for both supported architectures;
- exact tenant/workspace/purpose and read-token profiles;
- deterministic safe and seeded negative PII/active-content fixtures;
- declared owner share of the 4-vCPU/8-GB/25-GB minimum image-mode platform reference host; and
- prior known-good owner fragment/service-contract/receipt digests.

Missing or incompatible input returns RED/decision-required. It cannot be replaced with a Portal mock,
external model/provider, unpinned tag, sibling edit or favorable readiness fallback.

## Required output

`OmniscienceLocalRuntimeReceipt` contains at least:

```text
profile/revision; sp86_release_digest; mode/reproducible; git/image/configuration digests;
service_contract/rendered_fragment/service/network/volume/host_binding inventory digests;
local_model/dependency/readiness digests; tenant/pw0/provider-severance evidence refs;
persistence/resource/rollback evidence refs; availability_class=development-single-host;
ha_qualified=false; activation_authority=none
```

This is an Omniscience owner-local development receipt. It is not Portal evidence, platform
qualification, production activation or HA proof.
