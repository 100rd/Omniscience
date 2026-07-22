# PW0 fail-closed PII boundary (task-sp-61-pii-wall-pw0)

Governing: [ADR-0020](../decisions/0020-adopt-distributed-pii-wall.md) ·
[genai-enablement ADR-0018](../../../genai-enablement/docs/decisions/0018-pii-wall-purpose-bound-data-boundary.md) ·
[SPEC-PII](../../specs/SPEC-PII-pii-wall-ingestion-lifecycle.md).

**Status:** development / non-live. No profile is activated, no live personal data or
provider call is involved, and this boundary is not wired into
`omniscience_server.ingestion.*` -- see "Non-activation boundary" below.

## What this is

`IngestPIIGate` (`packages/core/src/omniscience_core/privacy/gate.py`) is the PW0
admission boundary: it evaluates a `DataEnvelope` against a `PIIPolicyBundle` and a
`Detector`, and returns a `GateDecision` whose `disposition` is one of:

| Disposition | Meaning | Sink call permitted? |
|---|---|---|
| `admitted` | `public`/`internal_non_personal`, or `personal`/`sensitive_personal` that is deterministically `redacted` | yes |
| `quarantined` | classified but not PW0-admitted; held in the sealed `QuarantineStore` | no |
| `parked` | the gate itself could not reach a trustworthy decision | no |

`Pw0Boundary` (`apps/server/src/omniscience_server/privacy/boundary.py`) is the only
way a caller invokes a protected sink (`OrdinaryStoreSink`, `ParseSink`, `ChunkSink`,
`EmbedSink`, `ProjectionSink` in `sinks.py`): `guard_*` raises `Pw0BoundaryDeniedError`
and never calls the sink unless `decision.disposition == GateDisposition.ADMIT`.

## Why a decision "parks" (`disposition == "parked"`)

Every reason below fails closed -- there is no fallback branch that admits:

| `decision.reason` | Trigger | Where |
|---|---|---|
| `policy_skew` | pinned `schema_major`, `policy_revision_pin`, or `signer_trust_profile` no longer match the live `PIIPolicyBundle`, or the bundle is outside its `issued_at`/`expires_at` window | `policy.check_policy_skew` |
| `detector_failure` | `Detector.classify()` raised any exception | `gate.IngestPIIGate.evaluate` |
| `quarantine_unavailable` | classification resolved to quarantine, but `QuarantineStore.put()` raised `QuarantineUnavailableError` | `gate.IngestPIIGate._quarantine_or_park` |

A `quarantined` (not parked) decision means classification and quarantine both
succeeded, but the content is not PW0-safe -- `decision.quarantine_id` names the hold.

Every decision carries a `BoundaryReceipt` (`decision.receipt`): `disposition`,
`reason`, a digest of the envelope identity, `policy_revision`, `profile`, and a
`detail` tuple of closed-taxonomy strings -- never a raw value, subject reference, or
field payload. `receipts.scan_receipt_for_raw_leakage` is defense in depth against an
accidentally-added raw field.

## Lifecycle receipts (deletion/restore)

`lifecycle.build_deletion_receipt` always derives `disposition` from the supplied
per-store `store_states` (`lifecycle.aggregate_lifecycle`) -- there is no parameter
that lets a caller claim `"complete"` while a store is `pending`, `failed`,
`backup_pending`, `immutable_retention`, or `unavailable`. `"deleted"`/`"clean"` are
non-conformant literals outside the closed disposition enum and are always rejected
by `lifecycle.check_lifecycle_claim`.

## Operating the gate

```python
gate = IngestPIIGate(quarantine_store=my_quarantine_store, consumer_pin=my_consumer_pin)
decision = gate.evaluate(
    envelope=envelope,
    policy_bundle=live_policy_bundle,
    detector=my_detector,
    reference_now=utc_now_iso8601,
    receipt_id=new_receipt_id,
    produced_at=utc_now_iso8601,
)
boundary.guard_store(decision, my_store_sink, envelope)  # raises if not admitted
```

Escalation: a `parked` decision is an operational event, not silent ingestion loss --
alert on `reason in {"policy_skew", "detector_failure", "quarantine_unavailable"}` and
resolve the underlying dependency (policy publisher, detector service, quarantine
backend) rather than relaxing the gate.

## Re-verifying the SP-60 contract binding

```bash
python3 contracts/pii/tooling/verify_pin.py
python3 -m pytest tests/test_pii_contracts_pin.py -v
```

See [`contracts/pii/README.md`](../../contracts/pii/README.md) for what is vendored
and why.

## Non-activation boundary

- No live personal data, provider call, profile activation, production quarantine
  store, destructive deletion, or deployment is authorized by this boundary.
- `omniscience_server.ingestion.*` (the real Postgres/Neo4j/Qdrant/parser/embedder
  pipeline) is unchanged -- `sinks.py` defines protocols only; wiring the real
  adapters through `Pw0Boundary` is a separate, later change (SPEC-PII scopes
  "changing current ingestion runtime through this draft" out).
- `InMemoryQuarantineStore` is a disposable, process-local fixture, not the sealed
  production `QuarantineStore` ADR-0020 requires before activation.
