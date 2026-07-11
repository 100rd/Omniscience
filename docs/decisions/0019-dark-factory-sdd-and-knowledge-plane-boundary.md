# ADR-0019: Adopt Dark Factory SDD and fix the knowledge-plane boundary

- **Status**: Proposed
- **Date**: 2026-07-11
- **Deciders**: Omniscience human owners / CODEOWNERS
- **Governing proposal**: genai-enablement ADR-0009, PR 100rd/genai-enablement#34
- **Related**: ADR-0015, ADR-0016, ADR-0017

## Context

Omniscience is a real read-only retrieval product with strong data-consistency mechanisms:
Postgres authoritative records, transactional outbox projection, temporal graph/vector reads,
workspace isolation, citations, and live-store conformance. Its governance artifacts do not yet
match that implementation maturity:

- ADR ids collided and there was no decision index;
- Consilium reviews became de facto capability contracts;
- task SPEC formats differ, and a factory-generated SPEC was marked `ready` with `Scope: TBD`;
- the claimed `spec-watch` execution path does not exist in this repository;
- product documentation can drift from authoritative-ledger and projection semantics.

The organizational Dark Factory needs Omniscience, but only as a bounded evidence plane. An
eventually consistent knowledge projection cannot become the correctness oracle, policy engine, or
task-state owner that judges the factory consuming it.

## Decision

### D1 - Adopt ADR -> capability SPEC -> task SPEC -> evidence

Humans decide boundaries and irreversible choices in `docs/decisions/`. Capability SPECs in
`/specs` define executable requirements, fallbacks, interfaces, and probes. Task SPECs in
`docs/specs/` define one immutable work revision. Agents may draft any artifact but cannot accept an
ADR, mark a capability/task SPEC ready, redefine their acceptance probes, or verify their own work.

### D2 - Keep Omniscience a read-only, severable knowledge plane

Omniscience may provide planning context, citations, lineage, topology, freshness, confidence,
historical replay, and independently sourced incident returns. It does not own downstream task
lifecycle, policy verdicts, correctness criteria, autonomy state, merge decisions, infrastructure
apply, or agent Experience. Action Mode remains a separate product boundary.

A consumer pins the evidence revision/as-of and records it in its task evidence. Loss or staleness of
Omniscience degrades planning to direct authoritative sources; it cannot corrupt or stop an already
materialized task contract. No downstream GREEN verdict may depend solely on Omniscience's own
confidence score or projection state.

### D3 - Evidence fitness is explicit and fail-closed for consumers

Every retrieval response exposes workspace, source lineage, citations, effective `as_of`, freshness,
confidence provenance, and degradation/consistency state. Missing, stale, cross-source mixed-epoch,
uncalibrated, or unavailable evidence remains visible. A consumer requiring a stronger property must
route to direct source or human review; it cannot reinterpret absence as current evidence.

Contract conformance is the required repository merge result. Runtime evidence fitness is a separate
response property and downstream gate input, not a permanently red repository check.

### D4 - Postgres is the authoritative rebuild ledger; projections are replaceable

Postgres-owned documents, chunks, lineage, versions, entity/outbox records, and governance metadata
are the recovery source. Neo4j and Qdrant are query-optimized projections written through the outbox
single-writer path, except for the human-operated empty-store DR procedure in ADR-0018. Reconciliation,
checkpoint/epoch filtering, and DR drills prove projection fitness. Documentation must use this model
consistently and must not present all three stores as co-equal sources of truth.

### D5 - Task SDD depth is graded

Omniscience uses the organizational Quick/Standard/Full modes:

| Mode | Boundary | Required artifacts |
|---|---|---|
| Quick | R0/reversible edit inside an accepted capability | lightweight task SPEC |
| Standard | routine behavior change inside an accepted boundary | complete task SPEC with registered probes |
| Full | new source/trust boundary, cross-repo contract, data-authority, synthesis/action, tenant, oracle, or irreversible migration change | accepted ADR + ready capability SPEC + task SPEC |

Classification may only widen the mode. `ready` is human-owned and mechanically rejected when scope,
probes, rollback, provenance, or governing references are incomplete.

### D6 - Reviews are evidence, not authority

Consilium and specialist reviews may find defects and propose requirements. A load-bearing finding is
promoted into an ADR or stable `REQ-*` before agents treat it as an execution contract. Review labels,
comments, PR prose, and issue priority cannot silently change architecture.

### D7 - Bootstrap is manual and claim-limited

Until the local intake validator and CODEOWNERS readiness path are verified, capability and task SPECs
remain draft and implementation is manually started by a human. No absent `spec-watch` automation may
be claimed. Bootstrap expires only when real readiness provenance and trigger probes pass.

## Consequences

**Positive**

- Dark Factory consumers gain grounded context without coupling correctness to an eventual projection.
- Existing conformance mechanisms acquire stable human decisions and agent-readable contracts.
- Data authority, projection consistency, evidence fitness, and tenant isolation become reviewable units.
- Incomplete generated task prose can no longer authorize execution.

**Costs / risks**

- Existing review findings and legacy task docs require gradual migration.
- Human readiness review becomes a measurable throughput constraint.
- Direct-source fallback must remain operable and exercised by consumers.
- Postgres backup/restore becomes explicitly load-bearing and requires production-grade evidence.

## Alternatives considered

1. **Use Consilium reviews directly as specs** - rejected: review iterations have unstable ids and mixed authority.
2. **Treat Omniscience as the factory oracle** - rejected: producer-owned confidence and eventual projections are not external correctness.
3. **Let ready task specs contain placeholders** - rejected: readiness would become a label rather than an execution contract.
4. **Put action execution into Insight Mode** - rejected: it collapses the read-only security and product boundary.

## Implementation map

| Decision | Capability SPEC | Closure evidence |
|---|---|---|
| D1/D5/D7 | SPEC-IN | schema/readiness/provenance/mode/immutability probes |
| D2 | SPEC-KP | read-only + severance + no-oracle probes |
| D3 | SPEC-EV | evidence envelope, freshness/degradation and direct-source fallback probes |
| D4 | SPEC-SOT | single-writer, projection convergence and rebuild probes |
| D2/D3 | SPEC-ACL | workspace derivation and cross-tenant non-disclosure probes |
| D1/D3/D4 | SPEC-OPS | required conformance, benchmark, DR and documentation-drift evidence |

Human closure requires the six capability SPECs verified on real paths and at least one governed task
to complete from a human-ready immutable SPEC. Document presence is insufficient.
