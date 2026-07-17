---
id: gh-issue-350-production-ha
title: Implement and qualify the Omniscience production HA profile
status: ready
readiness:
  approvedBy: "@100rd"
  approvedAt: 2026-07-17
source: { kind: github, ref: "350", url: "https://github.com/100rd/Omniscience/issues/350" }
governingAdrs: [genai-enablement/ADR-0017, Omniscience/ADR-0019]
capabilitySpecs: [SPEC-OPS, SPEC-SOT]
sddMode: full
repo: 100rd/Omniscience
scope:
  include:
    - helm/omniscience/**
    - docs/architecture.md
    - docs/runbooks/production-ha.md
    - tests/test_helm*.py
    - tests/test_production_ha*.py
    - .github/workflows/ha-qualification.yml
  exclude:
    - apps/**
    - packages/**
    - docs/decisions/**
    - docs/specs/**
    - specs/**
    - graphify-out/**
    - terraform/**
acceptanceCriteria:
  - id: AC-HA-1
    requirement: The production profile requires a dedicated-account EKS target and external HA stateful services
    probe: production-ha-profile-schema
    expected: production render fails closed unless account, cluster, three-zone placement, external RDS, JetStream, Neo4j, and Qdrant evidence references are supplied
    groundTruth: Helm values schema, rendered objects, and immutable environment evidence references
  - id: AC-HA-2
    requirement: Application replicas, PDB, topology spread, autoscaling, and priority classes have a deterministic HA policy
    probe: production-ha-scheduling
    expected: at least three replicas span three zones with DoNotSchedule skew one, PDB maxUnavailable one, HPA minimum three, and an explicit critical-read priority class
    groundTruth: rendered Kubernetes objects and policy assertions
  - id: AC-HA-3
    requirement: Every stateful dependency has a separately reviewable topology and entitlement record
    probe: production-ha-stateful-profile
    expected: RDS is Multi-AZ authority, JetStream has three replicas, Qdrant is distributed, and Neo4j has an approved HA topology plus license or managed-service entitlement; missing evidence is RED
    groundTruth: provider topology observations, service configuration, and entitlement metadata
  - id: AC-HA-4
    requirement: The qualified profile survives one application pod, node, and availability-zone fault without ledger or outbox corruption
    probe: production-ha-failure-domain
    expected: declared availability SLO is measured for each fault and any breach remains RED rather than being waived by retry
    groundTruth: disposable-cluster fault traces, request observations, PostgreSQL ledger hashes, and JetStream acknowledgements
  - id: AC-HA-5
    requirement: Evaluation and single-replica profiles cannot be presented as production HA
    probe: production-ha-posture-separation
    expected: bundled stateful charts, missing topology evidence, or fewer than three application replicas produce evaluation or governed posture only
    groundTruth: negative Helm fixtures, rendered labels, documentation, and posture validator output
rollback: { kind: revert-pr, probe: restore-last-qualified-evaluation-profile }
---

## Outcome

Build a fail-closed production-HA qualification profile in the existing Helm chart. The profile makes
the separate-account/cluster boundary, failure domains, scheduling policy, stateful-service evidence,
and posture label mechanically reviewable. It must remain usable with either an approved managed or
self-hosted Neo4j/Qdrant topology; the task does not silently choose or purchase one.

## Execution order and decision returns

Start after MCP v1 contract conformance is GREEN. Implement schema/render/policy checks before any live
fault probe. The live probe runs only against an explicitly disposable or approved qualification
environment.

If Neo4j entitlement, Qdrant topology, environment identity, or a measured SLO is absent, complete the
portable profile and emit a decision return naming the missing evidence. Do not replace the missing
fact with a default and do not claim `production-ha`.

## Out of scope

- creating or mutating an AWS account, EKS cluster, DNS, IAM, or production secrets
- selecting or purchasing Neo4j/Qdrant licenses or managed-service plans
- destructive stateful failover against a production environment
- changing the accepted ADRs, capability SPECs, this ready revision, or its probes

The human-ready revision is immutable during execution.
