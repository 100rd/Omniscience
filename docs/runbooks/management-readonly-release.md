# management-readonly-v1 release qualification (task-sp-86-management-readonly-release)

Governing: [ADR-0022](../decisions/0022-management-readonly-runtime-release.md) ·
[genai-enablement runtime profile](../../../genai-enablement/docs/synchronized-platform/profiles/management-readonly-v1.md) ·
[task spec](../specs/task-sp-86-management-readonly-release.md).

**Status:** producer release evidence / non-live. This packages the already-implemented
SP-10 (`SPEC-MCP`), SP-61 (`SPEC-PII`/PW0), and SP-81 (`SPEC-MCTX`) slices into one
immutable release candidate. It is not a deployment activation, privacy verdict, or
production-readiness claim -- see "Non-activation boundary" below.

## What this release is

`OmniscienceManagementReadOnlyRelease` binds the exact Git commit, buildable image
digest, Helm chart digest, MCP/management-context contract manifests, schema-set
digest, pinned SP-60 PW0 policy digest, dependency/readiness profile, severance
fixture kit, and a language-neutral (Go) consumer kit into one content-addressed
object. `scripts/qualify_management_readonly_release.py` builds and self-validates it.

```text
profile_id / profile_revision        management-readonly-v1 / 1.0.0
git_commit                            exact 40-hex HEAD
image_digest / chart_digest           buildable content digests (see "External gates")
configuration_digest                  over app.py, health.py, helm values.yaml
mcp_manifest_digest                   contracts/mcp/pin.json closure digest
management_context_manifest_digest    contracts/management/pin.json closure digest
schema_set_digest                     mcp + management + pii schema closure digest
pii_policy_revision / _digest         pinned SP-60 policy revision/digest
pw0_receipt_refs                      pointers to the seeded-leak evidence
auth_token_profile                    omniscience-mcp-read-v1
dependency_profile                    sp10/sp60/sp61/sp81 digests + omnius: not_deployed
readiness_contract                    /health, /ready, /metrics
severance_fixture_digest              contracts/releases/.../severance-fixtures.json digest
language_neutral_consumer_kit_digest  contracts/releases/.../consumer-kit-go/ digest
evidence_refs                         paths to the test suites and evidence directory
rollback_release_digest               "none:first-candidate" for this, the first release
```

## Running the qualification

```bash
# AC-SP86-1: release-lock digest re-derivation (prints the full JSON report)
.venv/bin/python scripts/qualify_management_readonly_release.py

# Persist the release lock as evidence under contracts/releases/management-readonly-v1/evidence/
.venv/bin/python scripts/qualify_management_readonly_release.py --write

# AC-SP86-2..5: runtime conformance, PW0 seeded-leak, severance kit, ops/rollback
.venv/bin/python -m pytest tests/release/management_readonly tests/test_management_readonly_release_lock.py -v

# AC-SP86-6: language-neutral Go consumer kit
cd contracts/releases/management-readonly-v1/consumer-kit-go
go vet ./... && go build ./... && go test ./... -v

# No-regression: SP-61 PW0 and SP-81 management-context suites still pass
.venv/bin/python -m pytest \
  tests/test_pii_wall_admission.py tests/test_pii_wall_fail_closed.py \
  tests/test_pii_wall_lifecycle.py tests/test_pii_contracts_pin.py tests/privacy \
  tests/test_management_context_scope_and_replay.py \
  tests/test_management_context_authority_and_pii.py \
  tests/test_management_context_severance.py \
  tests/test_management_context_boundary.py tests/management \
  tests/test_health.py -v

# Chart + Dockerfile gates
helm lint helm/omniscience
docker build -t omniscience:management-readonly-v1 .
```

`.github/workflows/management-readonly-release.yml` runs all of the above (except
a live registry push) on every PR/push touching this release's writable scope.

## A RED `scoped_worktree_dirty` reason is expected before commit

`scripts/qualify_management_readonly_release.py` scopes its Git-cleanliness check to
this task's own writable paths (never the ~420 pre-existing, unrelated
`graphify-out/` cache entries this repository already carries). While this task's own
files are staged but uncommitted, the qualify script correctly reports
`scoped_worktree_dirty` -- that is the release lock doing its job (AC-SP86-1: "dirty
inputs are RED"), not a defect. Re-run after commit to confirm the release lock goes
GREEN against the committed tree.

## Rollback

[`contracts/releases/management-readonly-v1/rollback.json`](../../contracts/releases/management-readonly-v1/rollback.json)
names the exact rollback contract (`kind: revert-pr`,
`probe: restore-prior-omniscience-release-digests-and-reject-unqualified-candidate`).
This is the first `management-readonly-v1` release, so `prior_release_digest` is
`null` (documented, not omitted) -- see that file's `procedure` list for what a
future rollback from a second release must do.

`tests/release/management_readonly/test_ops_rollback.py::test_release_lock_digests_are_exactly_reproducible`
exercises the prerequisite property directly: re-running the qualify harness against
the same tree always reproduces byte-identical digests.

## External / pending gates

- **Live OCI registry push.** `image_digest` in the release lock is a *buildable*
  content digest over `Dockerfile` bytes, not a live registry digest. The task
  spec's `evidenceDestination: ci-artifact://synchronized-platform/task-sp-86-management-readonly-release/`
  names a real `docker build` + `docker push` as a later, externally-authorized step
  -- see `pending.image_registry_digest` in every release-lock report.
- **Live consumer canary and human-reviewed severance drill.** SP-87 (Barbarossa)
  and SP-88 (Platform Portal) consume this release and return their own receipts;
  Omniscience validates supplied receipts read-only and cannot create, repair, or
  sign on a consumer's behalf (ADR-0022).
- **SP-90 (Barbarossa Go runtime).** Independent of this release; SP-87 later binds
  both immutable receipts.

## Non-activation boundary

- No credential, cloud key, live consumer pin, provider/model call, Barbarossa
  decision, deployment, or production-readiness claim.
- The release is producer evidence only: it does not grant deployment activation
  or management authority (ADR-0022 OMR-8).
- `/ready` (`apps/server/src/omniscience_server/routes/health.py`) reports
  dependency health only -- never a management verdict, action recommendation, or
  authorization (`tests/release/management_readonly/test_ops_rollback.py::test_ready_never_returns_a_management_verdict_field`
  enforces this).
- `helm/omniscience`'s `managementReadonly.enabled` block is opt-in and additive;
  the default install (`enabled: false`) is unaffected. When enabled, the chart
  fails closed (`templates/_management_readonly_guards.tpl`) unless the release
  identity and every ADR-0022 configuration invariant is supplied.
- Every fixture consumed by the release-qualification suites is disposable and
  synthetic (`dev-fixture-*`/`sev-*` identifiers); none is live personal data or a
  real evidence source.
