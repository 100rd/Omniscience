# ADR-0023: Publish an owner-contained local runtime for the Management Read-Only profile

Status: accepted
- **Date:** 2026-07-29
- **Deciders:** platform owner and Omniscience owner
- **Governing decisions:** `genai-enablement` ADR-0018, ADR-0021, ADR-0022, ADR-0024 <!-- stale-arch-allow: external ADR namespace -->
- **Related local decisions:** ADR-0020, ADR-0021 and ADR-0022

## Context

Omniscience already has full, lite and image-based Compose variants. They use generic resource names
and host ports and represent an independently operated Omniscience installation, not an includable
synchronized-platform owner fragment. The integrated local profile also needs exact readiness,
namespaced resources, a language-neutral service contract, deterministic safe fixtures and an
immutable handoff receipt for Portal and `genai-enablement`.

The owner fragment must retain Omniscience authority. A parent Compose file may connect an approved
read consumer, but cannot copy migrations, change dependency semantics, bypass PW0 or turn local
fixtures into production truth.

## Decision

Omniscience publishes `deploy/compose/management-readonly-local/compose.yaml` plus an optional
`compose.source.yaml` override and `contracts/releases/management-readonly-local-v1/service-contract.json`.
The fragment is independently runnable and may be included by the exact `genai-enablement` SP-98
composition.

The selected local closure is the lite functional runtime:

```text
omniscience-api -> omniscience-postgres
                 omniscience-nats
                 omniscience-neo4j
                 omniscience-qdrant
omniscience-admin -> omniscience-api
```

Embeddings run in-process from a pinned local model/cache. Discovery, reconcile, scheduler and retention
workers are disabled unless explicitly selected by a later owner profile. Ollama, Caddy, backup sidecar,
external model/provider and cloud connector are absent from the steady local closure.

Every service/network/volume/config key is `omniscience-*`. Only the admin UI is published by default on
a configurable loopback port. The API joins one owner-read network for approved consumers and one
Omniscience-private network for stores. No store or broker is host-published by default.

Readiness covers every load-bearing selected dependency, migrations and PW0 policy initialization. A
process that is live while PostgreSQL, NATS, Neo4j or Qdrant is unavailable remains unready. Disabled
workers are reported `not_selected`, never healthy. Health output is non-identifying and carries the
service-contract/profile revision.

One-shot bootstrap may ingest only deterministic synthetic non-PII fixtures through the same public
admission/PW0 path used by ordinary owner ingestion. A negative fixture job proves that seeded PII,
reversible tokens and active content reach no durable/derived/provider/export sink. Bootstrap has no
steady-state route or Portal credential.

The fragment supports exact image-digest mode and a source-build override. Source mode records the Git
commit and dirty flag and cannot claim reproducibility when dirty. Both modes produce an
`OmniscienceLocalRuntimeReceipt` for SP-97/SP-98; neither receipt activates SP-86 or a deployment.

## Runtime invariants

- **OML-1:** the fragment runs independently and remains wholly owned by Omniscience.
- **OML-2:** namespaced resources introduce no collision with Portal or Barbarossa.
- **OML-3:** selected readiness is dependency-aware across PostgreSQL, NATS, Neo4j and Qdrant.
- **OML-4:** local embeddings make no provider/model API call and use a pinned model identity.
- **OML-5:** PW0 precedes bootstrap, persistence, parsing, chunking, embedding, graph/vector, telemetry,
  archive and response boundaries.
- **OML-6:** consumer access is tenant/workspace/purpose-bound and read-only.
- **OML-7:** Omnius, selected-owner mocks, generic query, action/effect and owner mutation routes are absent.
- **OML-8:** normal stop/restart preserves owner volumes; reset is separate and explicit.
- **OML-9:** the receipt declares `availability_class=development-single-host`, `ha_qualified=false`
  and `activation_authority=none`.

## Development authority

This decision authorizes the bounded SP-95 local packaging and disposable qualification. It does not
authorize production data, raw PII, external providers, production credentials, infrastructure
mutation, Portal/Barbarossa writes, Omnius, managed effects, HA evidence or deployment activation.
