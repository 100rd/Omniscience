# ADR 0002 — Connector framework, not a published SDK (for now)

- **Status**: Accepted
- **Date**: 2026-04-17
- **Supersedes**: none

## Context

The term "Connector SDK" appeared in early docs ([docs/api/connector-sdk.md](../api/connector-sdk.md), issue [#5](https://github.com/100rd/Omniscience/issues/5)). Strictly speaking, an SDK is a published package with stable API, semver guarantees, and external-developer-targeted documentation — so that third parties can write connectors without forking Omniscience.

That is a **significant** ongoing commitment: stability, backward compatibility, versioning, examples, CI matrix against multiple framework versions, etc.

## Decision

For v0.1 and v0.2, the connector mechanism is an **internal framework**, not a published SDK. All connectors live in the Omniscience monorepo under `packages/connectors/`. Adding a new source type = PR against Omniscience.

Published SDK (separate `omniscience-sdk` PyPI package) is deferred. Revisit triggers listed below.

## Rationale

- **Smaller surface area** during rapid iteration — interface can evolve freely
- **Zero maintenance cost** for external versions, downstream breakage, or CI against older SDK versions
- **Consolidated learning** — we figure out what the right abstraction is by building several connectors ourselves, not by committing to an interface prematurely
- **Contributors contribute directly** — a PR in one repo is strictly easier than "install our SDK, write against it, test against our server, upstream back" flow

## Revisit triggers

Re-evaluate when any of these is true:

- External developer(s) explicitly request to ship a connector without merging into Omniscience
- 3+ different people outside the core team have contributed connectors (signal: ecosystem forming)
- A company needs to ship a proprietary / internal connector that cannot be upstreamed (licensing, IP, compliance)
- API stability is demonstrably reached (6+ months without breaking changes)

When triggered, extract to `packages/connectors/omniscience-connectors-sdk` → publish to PyPI → semver from that point.

## Consequences

- Doc `docs/api/connector-sdk.md` is renamed conceptually to "Connector framework". For now we keep the file path to avoid breaking existing links and add a disclaimer at the top.
- Issue [#5](https://github.com/100rd/Omniscience/issues/5) title adjusted in description to "Connector framework".
- No `@public` API annotations, no semver commitment. Breaking changes allowed, with migration notes in release notes.

## Alternatives rejected

### Publish SDK day-one

Rejected. Classic premature standardization — we'd freeze an interface we don't yet fully understand. Every real-world SDK-first design has iterated its interface post-launch; doing that with semver is costly.

### Full fork model

"Connectors live in their own repos, users fork Omniscience to add them." Rejected: this turns Omniscience into a generator/scaffold, not a product. Users want batteries-included.
