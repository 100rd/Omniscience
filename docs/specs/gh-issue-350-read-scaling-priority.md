---
id: gh-issue-350-read-scaling-priority
title: Implement shared admission, read scaling, and an SRE priority lane
status: ready
readiness:
  approvedBy: "@100rd"
  approvedAt: 2026-07-17
source: { kind: github, ref: "350", url: "https://github.com/100rd/Omniscience/issues/350" }
governingAdrs: [genai-enablement/ADR-0017, Omniscience/ADR-0019]
capabilitySpecs: [SPEC-MCP, SPEC-EV, SPEC-OPS]
sddMode: full
repo: 100rd/Omniscience
evidenceDestination: component-execution-index://docs/specs/execution-index.json#gh-issue-350-read-scaling-priority
scope:
  include:
    - apps/server/src/omniscience_server/rest/rate_limit.py
    - apps/server/src/omniscience_server/admission/**
    - packages/core/src/omniscience_core/config.py
    - helm/omniscience/**
    - docs/runbooks/read-admission.md
    - tests/test_admission*.py
    - tests/test_rest_api.py
    - tests/test_mcp*.py
    - .github/workflows/admission-qualification.yml
  exclude:
    - docs/decisions/**
    - docs/specs/**
    - specs/**
    - graphify-out/**
    - terraform/**
acceptanceCriteria:
  - id: AC-SCALE-1
    requirement: Every replica enforces one shared admission and rate-limit authority
    probe: shared-admission-conformance
    expected: aggregate token, workspace, and lane limits hold under adversarial concurrent requests with no process-local bypass
    groundTruth: shared-backend records, replica identities, accepted/rejected request totals, and negative configuration fixtures
  - id: AC-SCALE-2
    requirement: SRE incident reads have a separate bounded lane from background agent traffic
    probe: priority-lane-overload
    expected: required lane budgets and SLO thresholds are explicit inputs, background traffic degrades first, and neither lane can borrow unbounded capacity
    groundTruth: multi-replica overload, queue depth, latency, error, and fairness observations
  - id: AC-SCALE-3
    requirement: Overload returns consumer-visible degraded metadata and fallback
    probe: overload-degradation-contract
    expected: admitted responses and stable 429/503 errors preserve MCP v1 degraded/fallback semantics without false freshness or partial schema-invalid success
    groundTruth: MCP v1 schemas, contract fixtures, and live overload responses
  - id: AC-SCALE-4
    requirement: Shared-backend loss is fail-closed and bounded
    probe: admission-backend-failure
    expected: HA posture stops admitting background work, preserves only the explicitly bounded incident reserve when its authority is provable, and never falls back to per-process buckets
    groundTruth: backend fault traces, replica decisions, and aggregate request accounting
  - id: AC-SCALE-5
    requirement: Horizontal read claims require measured multi-replica evidence
    probe: horizontal-read-qualification
    expected: two or more application replicas pass skew, isolation, fairness, and fallback probes; single-replica or unmeasured profiles remain non-HA
    groundTruth: rendered topology, load-generator manifest, and content-addressed qualification report
rollback: { kind: revert-pr, probe: restore-single-replica-admission-profile }
---

## Outcome

Replace the current process-local read admission path with a shared, lease/atomic-operation based
interface, add a separately observable SRE incident lane, and make multi-replica qualification
reproducible. The implementation must keep the backend behind a narrow interface and a disabled
activation gate until a human-approved environment profile identifies the concrete managed or
self-hosted backend.

## Execution order and required inputs

Start after the production-HA application profile renders and MCP v1 conformance is GREEN. Land the
interface, state-machine tests, metrics, negative configuration checks, and containerized qualification
before enabling more than one application replica.

Backend identity, lane budgets, queue bounds, latency/error thresholds, and failure reserve are required
environment inputs. Missing inputs fail boot or qualification for HA posture; they are not code
defaults. A backend recommendation may be returned for human disposition, but the executing agent
cannot accept a new infrastructure dependency on its own.

## Out of scope

- provisioning or activating a shared backend in production
- allowing consumer identity to bypass tenant, freshness, consistency, or fallback rules
- unbounded SRE priority, process-local fallback in HA posture, or automatic lane tuning
- changing the accepted ADRs, capability SPECs, this ready revision, or its probes

The human-ready revision is immutable during execution.
