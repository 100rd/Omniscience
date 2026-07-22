# Omniscience local PII contract binding (task-sp-61-pii-wall-pw0)

**Status:** development / non-live. No active policy, signing key, permit, or provider call lives here.
See [ADR-0020](../../docs/decisions/0020-adopt-distributed-pii-wall.md) and
[genai-enablement ADR-0018](../../../genai-enablement/docs/decisions/0018-pii-wall-purpose-bound-data-boundary.md).

Implements `task-sp-61-pii-wall-pw0`, scoped to `contracts/pii/**`,
`apps/server/src/omniscience_server/privacy/**`, `packages/core/src/omniscience_core/privacy/**`,
`tests/test_pii*.py`, `tests/privacy/**`, and `docs/runbooks/pii-wall.md` only.

## This is a binding, not a fork

`genai-enablement/contracts/pii/` is the SP-60 ground truth: the published `PIIPolicyBundle`,
`DataEnvelope`, `SanitizationReceipt`, `DeletionReceipt`, and `PrivacyCoverageEnvelope` shapes plus a
content-addressed manifest over them (ADR-0018 D1/D5). Omniscience does not import Python across
repositories and does not redefine those shapes; it vendors a byte-identical, partial copy of the
schemas its own PW0 gate consumes and pins their digest.

```text
contracts/pii/
  pin.json               ground-truth manifest digest + per-file sha256 this binding claims to match
  schemas/v1/            byte-identical copies of the 12 SP-60 schemas PW0 needs (no PIIPermit /
                         boundary-decision-receipt / compatibility-matrix -- PW0 never issues a permit)
  validator/             vendored dependency-free JSON Schema subset validator (schema_check.py)
  tooling/verify_pin.py  recomputes every vendored file's sha256 and compares it to pin.json
```

`packages/core/src/omniscience_core/privacy/` and `apps/server/src/omniscience_server/privacy/`
implement the actual PW0 enforcement logic (admission predicate, policy-skew/detector/quarantine
fail-closed paths, per-store lifecycle aggregation) as originally-authored Omniscience code -- ADR-0018
D9 assigns Omniscience ownership of pre-ingest classification, quarantine, and knowledge-store lifecycle
receipts. Only the *shape* is bound to SP-60; the enforcement code is local.

## Running the probes

```bash
python3 -m pytest tests/test_pii_wall_admission.py tests/test_pii_wall_fail_closed.py \
  tests/test_pii_wall_lifecycle.py tests/test_pii_contracts_pin.py tests/privacy -v
```

Re-verify the pin after any change to a vendored schema:

```bash
python3 contracts/pii/tooling/verify_pin.py
```

## Non-activation boundary

- No credential, cloud key, active policy, provider call, runtime-pipeline wiring, deletion, or export.
- `contracts/pii/validator/` only re-exports the vendored structural `schema_check.validate`; it issues
  no permit and publishes no policy.
- This binding does not change `apps/server/src/omniscience_server/ingestion/*` -- SPEC-PII scopes that
  out explicitly ("changing current ingestion runtime through this draft" is Out of scope).
- Every fixture consumed by the PW0 tests is disposable and synthetic (see `tests/privacy/fixtures.py`);
  none is live personal data.
