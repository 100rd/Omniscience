# `management-readonly-v1` release lock (task-sp-86-management-readonly-release)

**Status:** producer release evidence / non-live. This is a packaging step over the
already-implemented SP-10, SP-61, and SP-81 repository-local slices -- it publishes
one immutable release candidate, it does not deploy, activate, or make a privacy
verdict. See [ADR-0022](../../../docs/decisions/0022-management-readonly-runtime-release.md).

```text
contracts/releases/management-readonly-v1/
  README.md                  this file
  schemas/v1/
    release-lock.schema.json  OmniscienceManagementReadOnlyRelease shape
                              (docs/specs/task-sp-86-management-readonly-release.md
                              "Required output")
  severance-fixtures.json    AC-SP86-4 severance fixture kit (loss / token-expiry /
                              freshness-expiry / contract-skew), shared verbatim by
                              tests/release/management_readonly/severance_fixtures.py
                              and consumer-kit-go/
  rollback.json               AC-SP86-5 rollback contract; v1 is the first release,
                              so prior_release_digest is null (documented, not omitted)
  consumer-kit-go/            AC-SP86-6 language-neutral Go consumer kit
  evidence/                   qualify_management_readonly_release.py --write output
                              (content-addressed by the release lock's own digest;
                              never overwritten, only appended to)
```

## Regenerating the release lock

```bash
.venv/bin/python scripts/qualify_management_readonly_release.py --report
.venv/bin/python scripts/qualify_management_readonly_release.py --write
```

`--report` prints the full `OmniscienceManagementReadOnlyRelease` JSON plus a
per-AC (AC-SP86-1..6) GREEN/RED breakdown to stdout without touching the
filesystem. `--write` additionally persists the same object to
`evidence/release-lock.<git_commit>.json` -- see
`docs/runbooks/management-readonly-release.md` for the full qualification and
rollback procedure.

## What "immutable" means here

Every digest in the release lock is re-derived from files this repository
already tracks (or, for the still-uncommitted candidate in this working tree,
from the current worktree bytes with `git_worktree_scope_clean=false`
recorded honestly). Re-running the qualify script against the same clean Git
commit always reproduces byte-identical digests; running it against a dirty
worktree or a different commit never silently reuses a stale value.

`source_input_digest` hashes the sorted paths and bytes of tracked and
non-ignored untracked files copied by the Dockerfile (`apps/`, `packages/`,
`contracts/`, and the root build manifests). The scoped cleanliness gate covers
those same build inputs, plus the Dockerfile and Helm chart. Ignored caches such
as `.venv/` and graph outputs are excluded. The historical `image_digest`
field remains the Dockerfile-only digest for compatibility; it is not an OCI
image digest.

## External / pending gates

- `pending.image_registry_digest`: the **live** OCI registry digest only exists after a real `docker build` +
  `docker push` runs in CI (`evidenceDestination:
  ci-artifact://synchronized-platform/task-sp-86-management-readonly-release/`
  in the task spec). That push is an external gate this task cannot perform.
- Chart/OCI publication to a real registry, a live consumer canary, and the
  human-reviewed severance drill (SP-87/SP-88's job, per
  `docs/synchronized-platform/profiles/management-readonly-v1.md`
  "Handoff order") are all out of this producer task's scope.

## Non-activation boundary

- No credential, cloud key, live consumer pin, provider/model call, Barbarossa
  decision, deployment, or production-readiness claim.
- This directory does not change `apps/server/src/omniscience_server/mcp/**`,
  `packages/core/src/omniscience_core/management/**` (beyond what SP-86 itself
  adds under its own writable scope), or any live infrastructure.
- Every fixture consumed by the severance kit is disposable and synthetic
  (`dev-fixture-*`/`sev-*` identifiers); none is live personal data or a real
  evidence source.
