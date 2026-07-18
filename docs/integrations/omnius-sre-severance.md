# Consumer severance: how Omnius and the SRE harness must react to `meta`

> This page is for engineers implementing or reviewing severance/fallback
> handling in a consumer of the Omniscience MCP v1 contract (Omnius, the
> SRE harness, or any other agent that calls Omniscience tools). It
> describes the decision procedure a conformant consumer must follow, not
> how Omniscience computes `meta` internally.
>
> Producer-side conformance evidence (the fixture matrix every state below
> is pinned against, and the read-only verifier that checks a receipt
> against it) lives in
> [`docs/runbooks/consumer-severance.md`](../runbooks/consumer-severance.md).

Every successful Omniscience MCP tool response carries a `meta` block
(see [`docs/api/mcp.md`](../api/mcp.md) for the full wire shape). Three of
its fields drive every severance decision: `freshness.status`,
`consistency.status`, and `fallback.{required,reason}`. `fallback.reason`
is the authoritative signal — it is the OR of the freshness and consistency
reasons, with a contract-pin mismatch always overriding both.

## The decision procedure

```
1. Is the response even parseable and does it carry a valid `meta` block?
   NO  -> planning_only. The response cannot gate anything (REQ-MCP-4).
   YES -> continue.

2. Is meta.fallback.required true?
   NO  -> use. Continue with the response as one input among several.
          (This is NOT authorization to skip step 4.)
   YES -> continue.

3. Is a direct, authoritative source (git, K8s, cloud API, incident
   system) reachable right now?
   YES -> direct_source_fallback. Query it instead of trusting this
          response.
   NO  -> park. Do not invent context from a stale/degraded/unverifiable
          response just because the direct source is also down.

4. Regardless of the outcome above — even `use` on perfectly fresh,
   converged, contract-pinned evidence — does this decision merge code,
   apply infrastructure, take an incident action, or make any other
   terminal decision?
   YES -> independent authority is required. Omniscience output, at any
          fitness level, is never a sufficient correctness or
          authorization oracle on its own (REQ-KP-3).
```

Step 4 is the part consumers most often get wrong: a `use` decision means
the evidence *passed the fitness gate*, not that it *passed an
authorization gate*. The two gates are orthogonal and both must be
satisfied independently before any terminal action.

## `fallback.reason` → decision table

| `fallback.reason` | What it means | Decision (direct source up) | Decision (direct source also down) |
|---|---|---|---|
| `null` (`fallback.required = false`) | Fresh, converged, contract-pinned evidence | `use` | n/a |
| `missing_lineage` | No source lineage extracted, or a used source is missing from the snapshot / synced after the evaluation boundary | `direct_source_fallback` | `park` |
| `never_synced` | A used source has never completed a sync | `direct_source_fallback` | `park` |
| `stale_source` | A used source's age exceeds its freshness SLA | `direct_source_fallback` | `park` |
| `source_degraded` | A used source's status is `error` | `direct_source_fallback` | `park` |
| `freshness_sla_unknown` | A used source has no configured freshness SLA | `direct_source_fallback` | `park` |
| `consistency_unknown` | No lag or watermark evidence for a projection-reading tool | `direct_source_fallback` | `park` |
| `projection_divergence` | Degraded subsystems reported, or non-zero projection lag | `direct_source_fallback` | `park` |
| `contract_mismatch` | Caller-signalled contract mismatch, or the source commit is not pinnable (dev sentinel / unparsed) — always overrides any freshness/consistency state | `direct_source_fallback` | `park` |
| *(any string not in this table)* | Schema/enum drift — a future Omniscience release added a reason your consumer doesn't recognize yet | `direct_source_fallback` (treat as `contract_mismatch`) | `park` |

The last row matters as much as the first eight: **never** treat an
unrecognized `fallback.reason` as safe-to-ignore. Fail closed, the same way
you would for `contract_mismatch`.

## REQ-MCP-4 payload-shape failures (before you even reach `meta`)

These never produce a `meta.fallback` state — they mean the response
itself is unusable:

| Condition | Decision |
|---|---|
| Response body is not finite JSON (`response_invalid`) | `direct_source_fallback` — never accept a truncated/invalid success |
| Response exceeds the fixed 80,000-byte budget (`response_too_large`) | `direct_source_fallback` |
| `meta` is missing from an otherwise successful payload | `planning_only` — usable for planning, never for a gate/terminal decision |
| `meta` is present but fails `meta.schema.json` validation | `planning_only` |

## Contract-pin verification failures (canary-class)

If your consumer runs its own release-pin verification against
Omniscience's MCP v1 contract bundle (mirroring what
`apps/server/src/omniscience_server/mcp/canary.py` does for Omniscience's
own CI), *any* fail-closed code from that verification — a manifest hash
mismatch, a tool-schema drift, a `contract_info` field mismatch, and so on
— means the contract identity itself cannot be trusted. Treat it exactly
like `contract_mismatch`: `direct_source_fallback` if a direct source is
reachable, `park` if not. Do not attempt to interpret *which* pin field
drifted as a reason to partially trust the response.

## Verifying your own severance conformance

Producer-side ground truth for every row above — plus an exhaustiveness
gate that fails if `fallback.reason`'s enum ever grows — lives in
[`tests/conformance/consumer_severance/`](../../tests/conformance/consumer_severance/)
in this repository. If you can build a receipt that runs your consumer's
actual decision logic against that matrix, the read-only verifier at
`scripts/check_consumer_severance.py` will tell you GREEN or RED; see
[`docs/runbooks/consumer-severance.md`](../runbooks/consumer-severance.md)
for the receipt format and how to run it.

## What this page does not cover

Live conformance checks against a real Omnius or SRE-harness revision
(AC-SEV-2), and a live severance drill proving already-materialized work
survives Omniscience being removed (AC-SEV-3), are **blocked on external
inputs not present in the Omniscience repository** — a real consumer git
revision, a real receipt, owner approval, and a named safe drill
environment. See
[AC-SEV-2/3 blocked-on-external](../runbooks/consumer-severance.md#ac-sev-23-blocked-on-external)
in the runbook for the full explanation and what has to arrive from
outside this repository before those can move.
