# Shared read-admission & SRE priority lane runbook

> Status: shared-admission interface, fail-closed state machine, server-derived
> SRE incident lane, and horizontal-read qualification harness shipped
> (AC-SCALE-1, AC-SCALE-2, AC-SCALE-3, AC-SCALE-4, AC-SCALE-5's repo-local
> scoring/report logic) — issue
> [#350](https://github.com/100rd/Omniscience/issues/350),
> [`gh-issue-350-read-scaling-priority`](../specs/gh-issue-350-read-scaling-priority.md).
> AC-SCALE-5's live 2+-replica measured evidence, acceptance of a concrete
> shared backend, and live backend-outage/fairness runs under real load are
> **blocked on external inputs** — see
> [Blocked on external — decision returns](#blocked-on-external--decision-returns)
> below.

## Table of contents

- [Posture model](#posture-model)
- [Scope boundaries](#scope-boundaries)
- [Supplying admission evidence](#supplying-admission-evidence)
- [Running the qualification checks](#running-the-qualification-checks)
- [Backend recommendation](#backend-recommendation)
- [Blocked on external — decision returns](#blocked-on-external--decision-returns)

## Posture model

Admission has **two independent gates**, deliberately not coupled to each
other:

| Gate | Field | Effect |
|---|---|---|
| Application backend selection | `Settings.admission_backend` (`packages/core/.../config.py`) | `"disabled"` (default) → `ProcessLocalAdmissionBackend`, the pre-#350 in-process token bucket, unchanged. Any other value → the fail-closed `SharedAdmissionBackend` stub (see below) behind `FailClosedAdmissionController`. |
| Chart evidence requirement | `production.admission.enabled` (`helm/omniscience/values.yaml`) | `false` (default) → `omniscience.admissionGuards` is a no-op and no `ADMISSION_*` env vars render. `true` → every admission evidence field is required or the render fails closed (`helm/omniscience/templates/_admission_guards.tpl`). |
| Lane-budget split | `Settings.admission_lane_incident_budget` / `admission_lane_background_budget` (`rest/rate_limit.py::_lane_capacity_and_bucket`) | Unset (default, both `None`) → every lane resolves to the single shared bucket at `rate_limit_rpm` — byte-for-byte the pre-#350 single-bucket behaviour. Setting either budget activates a separately-bounded bucket for that lane. |

The lane-budget split gate is **deliberately independent of `admission_backend`**, the same "two independent gates, not coupled" design used above: setting `ADMISSION_LANE_INCIDENT_BUDGET`/`ADMISSION_LANE_BACKGROUND_BUDGET` activates lane-partitioned rate-limiting on its own, even while `admission_backend` stays `"disabled"` — it does not require also selecting a shared backend. This is intentional (`tests/test_rest_api.py::test_incident_lane_has_separate_budget_from_background_lane` exercises exactly this combination end-to-end), not an oversight: an operator can adopt SRE-incident-lane fairness in the single-process MVP posture before ever provisioning a shared backend. The only coupling this runbook asks you to remember: leaving both lane-budget fields unset (the true zero-touch default) is what preserves pre-#350 behaviour — see `tests/test_rest_api.py::test_default_disabled_posture_has_no_lane_split` for the backward-compat proof.

`production.admission.enabled` is **independent of `production.enabled`**
(the production-HA gate from issue #350's earlier "production-HA" slice,
already merged). A production-HA deployment can run with admission left at
its process-local default; enabling admission evidence never retroactively
requires re-supplying HA evidence, and vice versa. This is a deliberate
compatibility boundary: the existing production-HA qualification suite
(`tests/test_production_ha_render.py`, `tests/test_helm_posture.py`) must
keep passing unmodified.

There is no separate "posture label" for admission the way
`omniscience.io/posture` models production-HA — admission is a narrower,
additive capability layered on top, not a parallel deployment posture.

## Scope boundaries

This slice's declared scope
(`docs/specs/gh-issue-350-read-scaling-priority.md`) is:
`apps/server/src/omniscience_server/rest/rate_limit.py`,
`apps/server/src/omniscience_server/admission/**`,
`packages/core/src/omniscience_core/config.py`, `helm/omniscience/**`, this
runbook, `tests/test_admission*.py`, `tests/test_rest_api.py`,
`tests/test_mcp*.py`, and this slice's own CI workflow. Two real gaps fall
outside that boundary and are flagged here rather than silently worked
around:

- **MCP tool-dispatch admission wiring is a follow-up.**
  `apps/server/src/omniscience_server/mcp/server.py` has zero admission
  coverage today (grepped: no `rate_limit`/`admission`/`429`/`503` hits) and
  is not in this slice's `scope.include`. REST is fully covered — every
  `rate_limit_dependency` call (today, only `/api/v1/search`) routes through
  the shared `AdmissionBackend`/`FailClosedAdmissionController` stack. A
  future change that touches `mcp/server.py` should call
  `admission.mcp_contract.raise_admission_error(outcome)` at the tool
  dispatch boundary — see that module's docstring for why it raises a
  stable `ValueError("<code>:<message>")` (the same convention every
  existing MCP rejection uses) rather than inventing a `meta.fallback`
  shape, which would violate the closed `fallback.reason` enum in
  `mcp/contracts/v1/schemas/meta.schema.json` (also out of this slice's
  scope to edit).
- **`rest/webhooks.py`'s `_source_buckets` is a second, independent
  process-local bucket** for inbound webhook ingestion — a different
  admission class (source ingestion, not consumer read admission) and not
  in `scope.include`. It is unaffected by this slice and was not migrated
  onto the shared `AdmissionBackend` Protocol; a future unification is a
  separate decision, not implied by AC-SCALE-1's "no process-local bypass"
  (which is scoped to *read* admission).
- **Workspace-scoped aggregate limits are not implemented.** AC-SCALE-1's
  `groundTruth` (spec line 35) names "aggregate token, workspace, and lane
  limits" as the admission dimensions; this slice ships token- and
  lane-scoped admission (bucket key `(lane, token_id)`,
  `admission/process_local.py`) but no workspace-level aggregate dimension.
  This is a pre-existing gap — the pre-#350 single-process limiter was also
  token-only — not something this build closed or silently dropped. A
  workspace-aggregate dimension would need to aggregate consumption across
  every token in a workspace, which in turn needs a backend that spans
  replicas (the same shared-backend decision-return as
  [Blocked on external](#blocked-on-external--decision-returns) below); it
  is flagged here explicitly so a future reader doesn't assume it was
  considered and intentionally deferred versus simply not noticed.

## Supplying admission evidence

Set `production.admission.enabled=true` and populate every field under
`production.admission` — all are **required** when enabled; an
empty/invalid one aborts the render and names the exact missing key
(`helm/omniscience/templates/_admission_guards.tpl`):

```yaml
production:
  admission:
    enabled: true
    backend: "shared-redis-lease"        # must name a real, non-"disabled" backend
    backendIdentity: "redis://admission.internal:6379/0"  # human-approved concrete identity
    laneIncidentBudget: 120               # SRE incident lane, requests/minute
    laneBackgroundBudget: 60              # background (search/agent) lane, requests/minute
    queueDepthMax: 500                    # bounded admission queue ceiling
    latencyThresholdMs: 250               # incident-lane p99 latency ceiling
    errorRateThreshold: 0.01              # incident-lane error-rate ceiling (fraction)
    failureReserveFraction: 0.2           # bounded incident reserve on backend loss (0,1]
```

These map onto `packages/core/src/omniscience_core/config.py`'s
`admission_*` `Settings` fields (env vars `ADMISSION_BACKEND`,
`ADMISSION_BACKEND_IDENTITY`, `ADMISSION_LANE_INCIDENT_BUDGET`,
`ADMISSION_LANE_BACKGROUND_BUDGET`, `ADMISSION_QUEUE_DEPTH_MAX`,
`ADMISSION_LATENCY_THRESHOLD_MS`, `ADMISSION_ERROR_RATE_THRESHOLD`,
`ADMISSION_FAILURE_RESERVE_FRACTION`) via `helm/omniscience/templates/configmap.yaml`.
The Python side has its own, independent fail-boot check
(`Settings._validate_admission_backend_requirements` — the first
Python-side fail-boot pydantic validator in this codebase): constructing
`Settings(admission_backend=<non-disabled>)` without every required field
raises `ValidationError` at boot, before any connection is attempted.

**Setting `production.admission.backend` to anything other than
`"disabled"` today activates `SharedAdmissionBackend`, an intentionally
unimplemented stub** (`apps/server/src/omniscience_server/admission/shared.py`).
Every call into it raises `AdmissionBackendUnavailableError`, which
`FailClosedAdmissionController` turns into the AC-SCALE-4 backend-loss
posture: background admission stops, incident admission is rejected unless
a `LeaseAuthority` proves a reserve grant (no real one is wired — see
[Blocked on external](#blocked-on-external--decision-returns)). This is
deliberate: the executing agent cannot accept a new infrastructure
dependency on its own (task-spec, verbatim), so activating a backend
identity today is a fail-closed action, never a silent fallback to
process-local state.

## Running the qualification checks

```bash
# 1. Structural lint — evaluation, production-HA, and production-HA+admission
helm lint helm/omniscience --set secrets.postgresPassword=x --set serviceAccount.create=false --set postgres.enabled=false --set retention.enabled=true
helm lint helm/omniscience <PROD_SET from ha-qualification.yml>
helm lint helm/omniscience <PROD_SET> --set production.admission.enabled=true --set production.admission.backend=shared-redis-lease --set production.admission.backendIdentity=x --set production.admission.laneIncidentBudget=120 --set production.admission.laneBackgroundBudget=60 --set production.admission.queueDepthMax=500 --set production.admission.latencyThresholdMs=250 --set production.admission.errorRateThreshold=0.01 --set production.admission.failureReserveFraction=0.2

# 2. Fail-closed guard proof — enabled without evidence must abort
helm template omniscience helm/omniscience <PROD_SET> --set production.admission.enabled=true
# => Error: execution error ...: production.admission.backend is required when production.admission.enabled=true (...)

# 3. Python-side unit/config/state-machine/qualification-scoring tests
uv run pytest tests/test_admission_backend.py tests/test_admission_lanes.py \
  tests/test_admission_fail_closed.py tests/test_admission_config.py \
  tests/test_admission_qualification.py tests/test_mcp_admission_contract.py -v

# 4. REST/MCP regression — this slice must not break existing rate-limit/MCP tests
uv run pytest tests/test_rest_api.py tests/test_mcp*.py -q
```

`.github/workflows/admission-qualification.yml` runs all of the above on
every PR/push touching `helm/omniscience/**`, `apps/server/.../admission/**`,
`rest/rate_limit.py`, `config.py`, or `tests/test_admission*.py`.

The qualification **report** shape (`admission.qualification.QualificationReport`)
is content-addressed exactly like `scripts/qualify_backup_restore.py`'s
`DrillManifest`: `report_id` self-addresses every other field (sha256), so a
tampered or partially-filled report cannot silently claim a different
identity. `build_qualification_report` composes four probes — skew,
isolation, fairness, fallback — each a pure function over explicit measured
inputs; `tests/test_admission_qualification.py` exercises all four against
synthetic fixtures. A report always resolves `RED` below two replicas,
regardless of individual probe status (AC-SCALE-5: "single-replica or
unmeasured profiles remain non-HA").

## Backend recommendation

Per the task spec, "a backend recommendation may be returned for human
disposition, but the executing agent cannot accept a new infrastructure
dependency on its own." Two candidates for the eventual
`SharedAdmissionBackend` implementation, for human evaluation — neither is
provisioned or wired here:

- **Redis, `SET NX PX` or a Lua CAS script.** Redis's `SET key value NX PX
  <ttl>` (or a small Lua script for a genuine atomic check-and-decrement
  token bucket) gives the single atomic primitive `AdmissionBackend.try_admit`
  needs, with well-understood operational characteristics and existing
  Kubernetes operators for HA Redis. The `LeaseAuthority` used for the
  AC-SCALE-4 incident reserve should be a *separate* mechanism from the
  Redis instance backing ordinary admission — reusing the same instance
  would mean a Redis outage takes down both the primary admission path and
  the only thing that could prove incident-reserve authority during that
  outage, defeating the point.
- **A managed rate-limiting/quota service** (e.g. a cloud provider's native
  distributed rate limiter), if the deployment already depends on that
  provider's ecosystem and wants to avoid operating another stateful
  service. Managed services vary in whether they expose a primitive precise
  enough for a real check-and-decrement lease — verify before committing.

Either choice, and the actual provisioning/activation, is a human decision
recorded via `production.admission.backend`/`backendIdentity` — this
runbook does not pre-select one.

## Blocked on external — decision returns

Per `docs/specs/gh-issue-350-read-scaling-priority.md` ("Execution order and
required inputs" / "Out of scope") and this workspace's approval rules, the
following are **decision returns**, not something this slice attempts or
fabricates:

1. **AC-SCALE-5's live 2+-replica measured evidence** (skew, isolation,
   fairness, fallback probes against real running replicas). CI here
   (`ubuntu-latest`, single runner, no Kubernetes cluster) cannot stand up
   2+ live application replicas behind a shared backend and measure real
   cross-replica behaviour. Repo-local work built: the four scoring
   functions (`score_skew`, `score_isolation`, `score_fairness`,
   `score_fallback`), the content-addressed report shape and its
   `report_id` self-addressing logic, and fixture-driven unit tests of all
   four against synthetic measurement data — the actual multi-replica run
   requires a real cluster and human-approved environment.
2. **Acceptance/provisioning of a concrete shared backend** — the spec is
   explicit: "the executing agent cannot accept a new infrastructure
   dependency on its own." Repo-local work produced the narrow
   `AdmissionBackend` Protocol, the process-local default implementation
   behind it, and a **fail-closed** `SharedAdmissionBackend` stub gated by
   `admission_backend`/`production.admission.enabled`, both defaulting to
   the disabled/off state. See [Backend recommendation](#backend-recommendation)
   above for candidates a human may choose from.
3. **AC-SCALE-4's fail-closed behaviour under a real backend outage** —
   repo-local work built and unit-tested the state machine
   (`FailClosedAdmissionController`) against injected/faked backend
   failures (unhealthy, raising, health-check-itself-raising). Proving it
   against a genuinely failing external shared store in a live
   multi-replica topology is the same live-environment gap as #1.
4. **AC-SCALE-2/3's live overload/priority-lane fairness under real
   concurrent multi-replica load** — load-shape *observations* and
   fairness-scoring *functions* are repo-local buildable/testable
   (`score_fairness` against synthetic queue-depth/latency/error inputs),
   but "multi-replica overload, queue depth, latency, error, and fairness
   observations" as literal `groundTruth` requires the same live cluster as
   #1.
5. Everything in the spec's own out-of-scope list remains out of scope
   regardless: provisioning/activating a shared backend in production,
   letting consumer identity bypass tenant/freshness/consistency/fallback
   rules (lane identity here is always server-derived from token scopes —
   see `admission/lanes.py`), unbounded SRE priority, process-local
   fallback in HA posture, automatic lane tuning, and any edit to the
   accepted ADRs/capability SPECs/this ready revision/its probes.
