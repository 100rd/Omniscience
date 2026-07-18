# Consumer-severance runbook

> Status: fixture matrix + verifier shipped (AC-SEV-1, AC-SEV-5) — issue
> [#350](https://github.com/100rd/Omniscience/issues/350),
> [`gh-issue-350-consumer-severance`](../specs/gh-issue-350-consumer-severance.md).
> AC-SEV-2 and AC-SEV-3 are **blocked on external inputs** — see
> [AC-SEV-2/3 blocked-on-external](#ac-sev-23-blocked-on-external) below.

This runbook documents how to run the producer-owned consumer-severance
conformance kit: the fixture matrix that pins every MCP v1 evidence-fitness
and contract-pin failure to a deterministic consumer decision
(`scripts/check_consumer_severance.py`, `tests/conformance/consumer_severance/`),
and the read-only verifier that checks a consumer's (Omnius, the SRE
harness) severance receipt against it.

## What this kit proves, and what it doesn't

- **Proves (AC-SEV-1, in this repo, GREEN today):** for every value of the
  closed `meta.fallback.reason` enum, every REQ-MCP-4 payload-shape
  failure, and a representative set of `canary.py` contract-pin fail-closed
  codes, there is exactly one correct consumer decision
  (`use` / `direct_source_fallback` / `park` / `planning_only`), and the
  matrix covers the enum 1:1 (an exhaustiveness gate re-reads
  `meta.schema.json` at test time and fails if the enum drifts).
- **Proves (AC-SEV-5, in this repo, GREEN today):** the verifier itself
  never edits, creates, commits, or pushes anything — on the GREEN path or
  any RED path — and it never accepts a live checkout of a consumer
  repository, only a revision string and a receipt file.
- **Does not prove:** that Omnius or the SRE harness actually implement this
  decision rule (AC-SEV-2), or that a live severance drill succeeds
  (AC-SEV-3). Those require real receipts from those repositories, which do
  not exist in this workspace — see below.

## Running the verifier against a real consumer revision

```bash
uv run python scripts/check_consumer_severance.py \
  --consumer-revision <40-hex immutable git commit from Omnius or the SRE harness> \
  --receipt <path to that consumer's severance-receipt.json>
```

Exit code `0` and `consumer-severance verification: GREEN` on stdout means
the receipt fully and correctly covers the fixture matrix at the exact
pinned revision. Any other outcome is exit code `1` and
`consumer-severance verification: RED (decision-return)` on stderr, followed
by one or more machine-readable reason codes (`revision_not_immutable:...`,
`receipt_absent`, `receipt_partial:missing=[...]`,
`decision_mismatch:<id>:expected=...:observed=...`, etc.).

The verifier is **read-only by construction**: it takes no path into a
consumer repository at all, only a revision string (data) and a receipt
file (data). It never shells out to `git`, never opens a network
connection, and never writes, creates, or deletes a file anywhere. This is
proven in `tests/test_consumer_severance_verifier.py` by fingerprinting the
verifier's own directory tree and the receipt's directory before and after
every GREEN and every RED run and asserting byte-for-byte equality.

## Severance receipt format

A consumer produces this JSON object by running the fixture matrix (either
by porting `tests/conformance/consumer_severance/fixtures.py`'s decision
table into its own test suite, or by calling this producer's
`fixture_matrix_sha256()` / `expected_decisions()` / `all_ids()` helpers
directly if it can import this repo) and recording, per fixture id, the
decision its own severance logic actually produced:

```json
{
  "consumer_revision": "9c8b7a6d5e4f3c2b1a0f9e8d7c6b5a4938271605",
  "fixture_matrix_sha256": "<sha256 of tests/conformance/consumer_severance/fixtures.py's canonical matrix>",
  "command": "uv run pytest tests/severance/test_omniscience_fitness_matrix.py -q",
  "results": [
    {"id": "fresh-converged-use", "decision": "use"},
    {"id": "missing-lineage-no-source-ids", "decision": "direct_source_fallback"}
  ]
}
```

| Field | Requirement |
|---|---|
| `consumer_revision` | Exact 40-hex git commit SHA. No `-dirty` suffix, no branch name, no working-tree state — an immutable, pinned revision only. Must match the `--consumer-revision` the verifier was invoked with. |
| `fixture_matrix_sha256` | Must equal this producer's current `fixture_matrix_sha256()` (content-addressed — a stale or hand-edited receipt fails closed rather than passing silently). |
| `command` | Non-empty string documenting exactly how the consumer ran the matrix — for audit trail, not machine-checked beyond presence. |
| `results` | Must cover every id in `all_ids()` exactly once — no partial coverage, no unexpected extra ids — each with the consumer's observed `decision` string. |

## What GREEN and RED mean

- **GREEN** — the full fitness matrix passed at an exact, immutable,
  pinned consumer revision. This is evidence the consumer's severance
  behavior conforms *as of that commit*. It is not evidence that any later
  commit still conforms — re-run against the new revision.
- **RED is a decision return, not a crash.** The verifier never raises an
  uncaught exception; every failure mode (absent receipt, dirty/mutable
  revision, stale digest, partial coverage, a single wrong decision)
  resolves to an explicit `RED` with a specific reason code. Treat RED the
  same way you would treat a failed CI gate: it blocks trusting that
  consumer's severance posture until fixed, but it is itself a successful,
  informative run of the verifier — not a bug in the verifier.

## Escalation

A RED receipt from Omnius or the SRE harness is **not** something this
Omniscience task (or any Omniscience agent) is authorized to fix directly —
per the task's own out-of-scope list, writing, committing, or pushing
changes in Omnius or the SRE repository is explicitly excluded. A RED
receipt should become a human-ready task spec filed in the *consumer's own
repository*, scoped to fixing that consumer's severance-decision logic
against this producer's fixture matrix, cross-referencing this runbook and
the exact RED reason codes from the verifier's stderr output.

## AC-SEV-2/3: blocked-on-external

Per `docs/specs/gh-issue-350-consumer-severance.md`:

> A missing consumer revision, direct-source profile, owner approval, or
> safe drill target produces a decision return and RED receipt. It does not
> authorize this agent to edit another repository.

This repository (Omniscience) has **no** Omnius or SRE-harness checkout, no
consumer git revision, and no consumer-produced receipt anywhere in its
tree. Concretely:

- **AC-SEV-2** ("Omnius and the SRE harness are checked from exact
  immutable consumer revisions... absent, dirty, mutable, or partial
  evidence is RED") cannot be exercised against real consumers from this
  workspace — there is nothing to point `--consumer-revision` /
  `--receipt` at. The verifier that *would* run this check is built and
  proven read-only (AC-SEV-5); running it against Omnius/SRE is a
  cross-repo, human-scheduled action.
- **AC-SEV-3** ("Removing Omniscience does not stop already materialized
  work... live Omnius and SRE harness drill traces") requires a named safe
  environment with an already-materialized task or incident and verified
  direct-source access. No such environment exists in this repo, and
  `PLATFORM.md` explicitly forbids an Omniscience-scoped agent from editing
  `genai-enablement` or `omnius`.

Both are therefore **decision-return / RED**, not GREEN, not a test
failure — the correct outcome given the evidence available. Do not
interpret a fixture-only conformance pass (this runbook's "Running the
verifier" section) as satisfying AC-SEV-2 or AC-SEV-3; it satisfies only
AC-SEV-1 and AC-SEV-5. Owner approval, a real consumer revision + receipt,
or a safe live-drill target must arrive from outside this repository before
either can move.

## Reference

- Fixture matrix and decision model:
  [`tests/conformance/consumer_severance/`](../../tests/conformance/consumer_severance/)
- Conformance tests: [`tests/test_consumer_severance_matrix.py`](../../tests/test_consumer_severance_matrix.py),
  [`tests/test_consumer_severance_verifier.py`](../../tests/test_consumer_severance_verifier.py)
- Verifier: [`scripts/check_consumer_severance.py`](../../scripts/check_consumer_severance.py)
- Consumer-facing decision procedure: [`docs/integrations/omnius-sre-severance.md`](../integrations/omnius-sre-severance.md)
- Ground-truth schemas (read-only, never imported as Python by this kit):
  `apps/server/src/omniscience_server/mcp/contracts/v1/schemas/meta.schema.json`,
  `response.schema.json`
