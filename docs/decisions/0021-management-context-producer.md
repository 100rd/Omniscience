# ADR-0021: Publish severable management context without creating management authority

- **Status:** Accepted
- **Date:** 2026-07-21
- **Deciders:** platform owner and Omniscience owner
- **Governing decisions:** `genai-enablement` ADR-0017 and ADR-0020

## Context

Barbarossa agents and domain evaluators need cited knowledge, change history and knowledge-quality
conditions. If Omniscience output became an observation, verdict, approval or sole runtime dependency,
the optional knowledge plane would become management authority.

## Decision

Omniscience owns a versioned `ManagementContextBundle` and `KnowledgeQualitySnapshot` producer. Every
response binds tenant/workspace, subject and purpose, request and contract revisions, source citations,
coverage, freshness, conflicts, projection state, policy/PII receipt and integrity.

The producer returns evidence fitness and source health, never a domain outcome, priority, incident,
risk acceptance, action recommendation, approval or verification. Consumers must operate through a
typed severed/unavailable path using their direct authoritative sources.

Authorization is least-privilege, read-only and purpose scoped. The bundle contains only fields admitted
for the requested management purpose; no generic graph query, raw PII, active content or owner credential
is exposed.

## Development authority

This decision authorizes contract/schema, fixtures, read-only producer code and severance conformance.
It does not authorize live customer data, a provider/model call, a Barbarossa decision, a deployment or
production readiness.

