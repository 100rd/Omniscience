# SPEC-OPS: Operational Evidence & Trust Gates
Status: ready · Depends on: SPEC-SOT, SPEC-EV, SPEC-ACL
Readiness: human-approved by @100rd on 2026-07-11 under accepted ADR-0019

## Governing ADRs

- ADR-0018 - DR exception and verification
- ADR-0019 (accepted) - contract conformance and honest evidence

## Goal

Make every product/recovery claim continuously evidenced by required, observable, independently
failing gates rather than workflow presence or review prose.

## Requirements

[REQ-OPS-1] `main` requires lint/format/doc-drift, strict typecheck, unit/integration coverage,
dependency audit policy, capability conformance, and live-store conformance as branch checks. **Fallback:**
unconfigured required contexts block the production-ready claim and alert repository owners.

[REQ-OPS-2] At least one human approval is required for ADR/SPEC readiness, governance/security paths,
and production-impacting changes; execution authors cannot self-approve. **Fallback:** missing CODEOWNERS
or branch rule keeps autonomy/manual landing disabled.

[REQ-OPS-3] Scheduled Benchmark and DR workflows fail loudly on setup/health/migration errors and publish
artifacts only from completed runs. **Fallback:** missing artifact is failure, not zero/last-known success.

[REQ-OPS-4] Workflow dependencies, external images, actions, schemas, and benchmark vendors are version/
digest pinned with real provenance. **Fallback:** unverifiable pin blocks live evidence while deterministic
mock regression may continue.

[REQ-OPS-5] Live conformance uses supported versions aligned with deployment or records the tested
compatibility matrix. **Fallback:** incompatible store/client warning prevents production certification.

[REQ-OPS-6] Evaluation/lite, governed, and production-HA deployment postures have explicit claims and
required evidence; disabling reconciliation/retention cannot be presented as production healthy.
**Fallback:** unknown posture is evaluation-only.

[REQ-OPS-7] Availability/freshness SLOs cover API, ingestion lag, projection convergence, source staleness,
epoch suppression, backup, and restore. **Fallback:** missing SLI disables SLO compliance claims.

[REQ-OPS-8] Documentation status, store authority, tool surface, connector maturity, and roadmap are
checked against code/registry facts. **Fallback:** detected drift fails contract conformance.

[REQ-OPS-9] Consecutive scheduled failures create a human-visible incident/issue with owner and elapsed
time; silence is not evidence of health. **Fallback:** unavailable notification channel uses a second
independent wake path.

## Interfaces

```text
ContractConformance.evaluate(commit) -> pass|fail
LiveConformance.evaluate(environment, revisions) -> EvidenceReport
OperationalPosture.certify(scope, evidence) -> evaluation|governed|production-ha
```

## Verification

- P-OPS-1 queries repository rules and proves every named required context blocks a failing PR.
- P-OPS-2 attempts author self-approval/governance merge and is denied.
- P-OPS-3 breaks image/env/health/Alembic and observes failed run with no success artifact.
- P-OPS-4 validates official action/image/vendor refs and immutable digests.
- P-OPS-5 runs store contract tests against the declared support matrix without compatibility warnings.
- P-OPS-6 starts lite with critical workers off and proves posture is evaluation, not production.
- P-OPS-7 injects API/ingestion/projection/source/backup failures and observes each SLI/alert.
- P-OPS-8 mutates status/authority/tool docs and observes deterministic drift failure.
- P-OPS-9 simulates repeated scheduled failures and verifies primary plus secondary human wake.
