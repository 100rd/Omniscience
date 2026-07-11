# Task SPEC Queue

This directory contains bounded task contracts. GitHub issues and reviews are intake sources; only a
human-ready committed task SPEC may authorize agent execution after SPEC-IN is implemented and verified.

Legacy prose specs without frontmatter are treated as drafts until migrated.

## Template

```yaml
---
id: stable-task-id
title: Bounded outcome
status: draft
source:
  kind: github|human
  ref: source-id
  url: source-url
governingAdrs: [Omniscience/ADR-XXXX]
capabilitySpecs: [SPEC-XX]
sddMode: quick|standard|full
repo: 100rd/Omniscience
scope:
  include: []
  exclude: [docs/decisions/**, specs/**]
acceptanceCriteria:
  - id: AC-1
    requirement: observable condition
    probe: registered-probe-id
    expected: typed expected result
    groundTruth: external source of truth
rollback:
  kind: revert-pr|disposable|compensator
  probe: registered-rollback-probe
---
```

Agents may draft. Only authorized humans may transition `draft -> ready`; the executing identity cannot
modify its ready revision, governing ADR/SPECs, probes, fixtures, policy, or waiver state.

