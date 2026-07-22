# ADR-0020: Adopt the distributed PII Wall at the Omniscience propagation boundary

Status: accepted
- **Date:** 2026-07-21
- **Deciders:** platform owner and Omniscience owner
- **Governing decision:** `genai-enablement` ADR-0018

## Context

Omniscience can decode, parse, persist, chunk, embed and project source material before a downstream
consumer sees it. A prompt/output filter cannot undo personal-data propagation into the authoritative
ledger, graph/vector projections, caches, logs, archives or providers.

## Decision

Omniscience adopts the shared PII taxonomy, PW0/PW1/PW2 profiles and receipt semantics. It verifies an
exact content-addressed policy bundle and enforces it before ordinary persistence, parsing propagation,
chunking, embedding, projection and retrieval. Unknown, stale, unsigned or incompatible policy blocks
new protected processing.

The component implements a sealed `QuarantineStore` interface isolated from ordinary knowledge stores.
Development uses deterministic disposable fixtures. A live backend, access path, encryption/retention
profile and operator authority are required environment inputs and have no permissive default.

Embedding and enrichment providers are explicit sinks. No provider is enabled for protected data unless
the policy bundle names its exact posture and fields; missing posture is denial, not local fallback.

Omniscience publishes only non-identifying coverage and lifecycle receipts. It does not declare legal
compliance, mint a platform-wide permit, or make Platform Portal an enforcement dependency.

## Development authority

This decision authorizes `SPEC-PII` contract, fixture, validator and fail-closed repository work. It does
not activate a profile, process live personal data, provision a quarantine backend or key, call a live
provider, delete durable data, deploy, or claim privacy coverage.
