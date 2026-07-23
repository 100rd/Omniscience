# Omniscience management-context contract (task-sp-81-management-context-v1)

**Status:** development / non-live. No active consumer pin, production token, or provider/model call
lives here. See [ADR-0021](../../docs/decisions/0021-management-context-producer.md),
[genai-enablement ADR-0017](../../../genai-enablement/docs/decisions/0017-omniscience-mcp-v1-contract-and-severance.md),
and [genai-enablement ADR-0020](../../../genai-enablement/docs/decisions/0020-barbarossa-continuous-management-plane.md).

Implements `task-sp-81-management-context-v1`, scoped to `contracts/management/**`,
`apps/server/src/omniscience_server/management/**`,
`packages/core/src/omniscience_core/management/**`, `tests/test_management_context*.py`,
`tests/management/**`, and `docs/api/management-context.md` only.

## This is an original Omniscience contract, not a vendored binding

Unlike `contracts/pii/` (which vendors the SP-60 ground-truth shape from `genai-enablement`),
these schemas have no external ground truth to bind to. ADR-0021 makes Omniscience the schema
owner of `ManagementContextBundle` and `KnowledgeQualitySnapshot`
(genai-enablement ADR-0020 D8: "Omniscience may publish versioned, cited `ManagementContextBundle`
records and ... the authoritative `KnowledgeQualitySnapshot`"). `pin.json` still freezes each
schema's own digest so drift is caught deterministically -- it just has no upstream manifest to
compare against.

```text
contracts/management/
  pin.json               digest freeze over every schema this repo owns
  schemas/v1/            ManagementContextRequest, ManagementContextBundle, ManagementCitation,
                         KnowledgeQualitySnapshot, ManagementContextCapabilityManifest, and the
                         closed fallback-required result shape
  validator/              dependency-free JSON Schema subset validator (schema_check.py)
  tooling/verify_pin.py   recomputes every schema file's sha256 and compares it to pin.json
```

`packages/core/src/omniscience_core/management/` implements the read-only producer (scope
authorization, PW0-aware field admission, orthogonal knowledge-quality axes, authority/PII
conformance scanning, contract-skew and severance handling). `apps/server/src/omniscience_server/management/`
wraps it as the server-side boundary. Only the *shape* is a stable contract here; the producer
logic is ordinary originally-authored code, same as every other component in this repository.

## Running the probes

```bash
python3 -m pytest tests/test_management_context_scope_and_replay.py \
  tests/test_management_context_authority_and_pii.py \
  tests/test_management_context_severance.py \
  tests/test_management_context_boundary.py tests/management -v
```

Re-verify the pin after any change to a schema:

```bash
python3 contracts/management/tooling/verify_pin.py
```

## Non-activation boundary

- No credential, cloud key, live consumer pin, provider/model call, Barbarossa decision,
  deployment, or production-readiness claim.
- The producer never emits availability, error-budget, incident, cost-opportunity, risk,
  compliance, action, approval, effect, or verification truth (SPEC-MCTX REQ-MCTX-5) -- see
  `packages/core/src/omniscience_core/management/taxonomy.py` `FORBIDDEN_AUTHORITY_FIELD_TOKENS`.
- The producer applies PW0 field admission (`omniscience_core.privacy.taxonomy.is_pw0_admitted`)
  before any citation content is assembled into a bundle; it never emits seeded PII, credentials,
  or active content (SPEC-MCTX REQ-MCTX-6).
- Every fixture consumed by these tests is disposable and synthetic
  (see `tests/management/fixtures.py`); none is live personal data or a real evidence source.
