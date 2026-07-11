# SPEC-IN: Task SPEC Intake
Status: ready · Depends on: none
Readiness: human-approved by @100rd on 2026-07-11 under accepted ADR-0019

## Governing ADRs

- genai-enablement ADR-0009 (accepted) - organizational ADR-to-SPEC governance
- Omniscience ADR-0019 (accepted) - local SDD authority and bootstrap

## Goal

Make one human-ready, immutable task SPEC the only executable unit of work. Issues and review findings
are inputs; they do not start implementation directly.

## Requirements

[REQ-IN-1] Only a committed task SPEC with human/CODEOWNER `ready` provenance may trigger work. Agent
drafts are permitted; agent readiness is rejected. **Fallback:** unknown provenance keeps the SPEC draft.

[REQ-IN-2] Ready SPECs use a closed schema containing id, source, governing ADRs, capability SPECs,
SDD mode, repo/scope, acceptance criteria, ground-truth sources, and rollback. **Fallback:** unknown or
missing fields block readiness rather than being dropped.

[REQ-IN-3] `TBD`, empty scope, string-only acceptance criteria, absolute workstation paths, or missing
probe/expected/ground-truth fields are invalid in `ready`. **Fallback:** downgrade to draft and emit all
validation findings.

[REQ-IN-4] Quick/Standard/Full mode is derived deterministically from boundary novelty, components,
data authority, tenant/security effect, synthesis/action surface, migration irreversibility, and oracle
change. **Fallback:** unknown or conflicting classification selects Full/human decision.

[REQ-IN-5] The executed task revision and referenced ADR/SPEC/probe definitions are content-addressed.
Workers read the pinned envelope, not mutable `main`. **Fallback:** missing revision or hash mismatch
parks before planning.

[REQ-IN-6] The execution identity cannot modify its ready SPEC, governing decisions, probes, benchmark
fixtures, tenant policy, branch protection, or waiver state. **Fallback:** unproven path protection blocks
execution.

[REQ-IN-7] Boundary discovery emits a decision-return and parks. A new human-ready revision repeats
validation/classification; the current task cannot widen itself. **Fallback:** no human response expires
the task without changing product state.

[REQ-IN-8] Intake claim key `(task_id, revision)` is idempotent. Duplicate issue, webhook, or git events
cannot spawn another implementation. **Fallback:** unavailable claim store blocks spawn.

[REQ-IN-9] Tracker write-back reports draft, ready, picked-up, decision-required, PR, and terminal states,
but tracker state never overrides git truth. **Fallback:** write-back retries and alerts without duplicate
execution.

[REQ-IN-10] Bootstrap is explicit: until readiness provenance and trigger probes pass, humans launch
approved work manually and every merge remains human-owned. **Fallback:** absent automation cannot be
described as active; bootstrap remains manual.

## Interfaces

```text
TaskSpecValidator.validate(path, commit_sha) -> ValidationResult
SddClassifier.select(spec, changed_boundary) -> quick|standard|full
TaskNormalizer.pin(valid_spec) -> SignedTaskEnvelope
TaskClaim.claim(task_id, revision) -> claimed|already_claimed
```

## Verification

- P-IN-1 rejects agent-authored readiness and accepts authorized human provenance.
- P-IN-2 mutates every required/schema field and receives stable fail-closed errors.
- P-IN-3 rejects the legacy `ready + Scope: TBD` fixture and absolute local repo paths.
- P-IN-4 widens a requested Quick task touching tenant/data authority to Full.
- P-IN-5 proves mutable head cannot alter a pinned execution envelope.
- P-IN-6 denies writes to SPEC/ADR/probe/policy paths from the worker identity.
- P-IN-7 scope/oracle expansion parks and only a new ready revision resumes.
- P-IN-8 delivers one revision 100 times and observes one execution claim.
- P-IN-9 injects tracker outage without changing task truth or duplicating work.
- P-IN-10 proves no `spec-watch` claim is exposed until a real registered trigger passes.
