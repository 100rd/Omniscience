# SPEC-EV: Retrieval Evidence Contract
Status: ready · Depends on: SPEC-SOT, SPEC-ACL
Readiness: human-approved by @100rd on 2026-07-11 under accepted ADR-0019

## Governing ADRs

- ADR-0004 - staged retrieval
- ADR-0008 - bitemporal semantics
- ADR-0017 - per-source epoch pin
- ADR-0019 (accepted) - explicit evidence fitness

## Goal

Return evidence whose origin, time, freshness, confidence, and consistency limitations are machine
visible so consumers cannot confuse a plausible projection with current ground truth.

## Requirements

[REQ-EV-1] Every result carries workspace, source/document/chunk or entity ids, citation URI, lineage,
and retrieval strategy. **Fallback:** missing provenance suppresses the item.

[REQ-EV-2] Every response carries requested/effective `as_of`, recorded/valid time, and per-source epoch
or an explicit unversioned marker. **Fallback:** a consumer requiring temporal proof rejects unversioned
items or routes to direct source.

[REQ-EV-3] Freshness is derived from source SLO and last successful ingestion, never response time.
**Fallback:** missing/stale freshness is explicit and cannot be called current.

[REQ-EV-4] Confidence includes strategy/calibration provenance. Placeholder or uncalibrated scores are
bands/labels, not precise probabilities. **Fallback:** missing calibration suppresses decimal confidence.

[REQ-EV-5] Degradation is typed: unavailable store, stale source, anchor miss, empty-map blackout,
unmapped/lag/cold-zero drop, partial source, or cross-source epoch mix. **Fallback:** unknown degradation
uses `degraded_unknown`, never healthy.

[REQ-EV-6] Strict epoch filtering is default. Relaxed mode is an explicit consumer request, appears in
the response, and cannot satisfy a strict downstream gate. **Fallback:** missing policy selects strict.

[REQ-EV-7] Multi-source responses disclose that per-source snapshots may differ and do not claim a
single causal cut. **Fallback:** consumers requiring a causal join query direct sources or park.

[REQ-EV-8] Retrieval enumeration/count paths use deterministic exact mechanisms rather than semantic
top-k. **Fallback:** unsupported exact enumeration returns unavailable, not an approximate count labelled exact.

[REQ-EV-9] Evidence envelopes are schema-versioned and backward-compatible; unknown major versions fail
closed in governed consumers. **Fallback:** raw payload remains inspectable but non-authoritative.

## Interfaces

```text
EvidenceEnvelopeV1 = {
  workspace, items[], citations[], lineage[], requested_as_of, effective_as_of,
  freshness, confidence_provenance, epoch_state, degradation[], schema_version
}
```

## Verification

- P-EV-1 removes citation/lineage fields and observes item suppression.
- P-EV-2 exercises current, historical, and unversioned temporal evidence.
- P-EV-3 makes ingestion stale while queries succeed and observes stale, not current.
- P-EV-4 removes calibration and proves precise confidence is suppressed.
- P-EV-5 injects each degradation cause and validates stable typed output/metrics.
- P-EV-6 proves relaxed results cannot satisfy a strict consumer contract.
- P-EV-7 composes two source epochs and exposes mixed-snapshot semantics.
- P-EV-8 compares exact enumeration to complete seeded ground truth.
- P-EV-9 rejects an unknown major envelope version without data loss or silent coercion.
