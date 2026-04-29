# Property tests — bitemporal contract and retention invariants

This directory contains the Wave 6 validation gate for epic
[#97](https://github.com/100rd/Omniscience/issues/97):

* `_simulator.py` — pure-Python reference implementation of the
  ADR-0008 + ADR-0009 contract.  Dependency-free.
* `_strategies.py` — `hypothesis` fixtures: multi-workspace, overlapping
  names, overlapping `valid_*` ranges, end-dated chains.
* `test_bitemporal_contract.py` — eight property tests for the
  ADR-0008 §5/§9 + ADR-0006 §6 invariants (issue
  [#138](https://github.com/100rd/Omniscience/issues/138)).
* `test_retention_invariants.py` — four property tests for the
  ADR-0009 §1/§2/§3 retention invariants.

Sibling test surfaces:

* `tests/integration/test_incident_demo_historical.py` — historical
  incident demo across hot / warm / archive tiers.
* `benchmarks/test_bitemporal_perf.py` — performance regression suite
  (CI sanity floor + opt-in perf-lab full-fixture run).

## Why a simulator?

The simulator captures the contracts laid out in ADR-0008 §1, §2, §3,
§5, §9 and ADR-0009 §1, §2, §3, §4 verbatim.  It is the test surface
that the property tests exercise.

* **Determinism** — `hypothesis` runs the same fixtures across local
  shrinks and CI replays without container churn.
* **Speed** — 120 examples per property, 12 properties, ~1.5k
  fixtures, runs in seconds.  The live-mode equivalent (gated below)
  is a fraction of the same coverage by example count and runs only
  when explicitly enabled.
* **Safety** — the simulator is the *contract*, not a copy of the
  Neo4j / Qdrant adapters.  When the simulator finds a property
  violation, the bug is in the contract; when the live-mode harness
  finds a violation that the simulator missed, the bug is in the
  adapter.  Both failure modes are useful.

## Live-mode opt-in

The live-mode tests exercise the same properties on real Neo4j and
Qdrant testcontainers.  They are gated behind:

| Env var | Effect |
|---|---|
| `OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1` | Enable live Neo4j live-mode tests. |
| `OMNISCIENCE_RUN_QDRANT_CONTRACT_TESTS=1` | Enable live Qdrant live-mode tests. |
| `OMNISCIENCE_RUN_BITEMPORAL_PERF=1` | Enable the 1M-node perf-lab benchmark. |

The CI lane runs only the simulator tests.  Live-mode is opt-in for two
reasons: testcontainers add minutes of wall-clock per run, and the
property suite's value is in the simulator's coverage, not in
re-running the same predicates against a live container.  The
live-mode tests are valuable for one purpose: proving the simulator
matches reality.  A follow-up sub-issue tracks wiring the live-mode
harness into the Wave 6 closer ([#139](https://github.com/100rd/Omniscience/issues/139)).

## Performance budget — CI vs perf-lab

Issue #138 specifies three quantitative budgets:

1. p95 `as_of=None` reads on a 1M-node fixture within 10% of
   pre-bitemporal baseline.
2. p95 `as_of=T` reads within 50% of `as_of=None`.
3. Retention worker p95 < 60s per workspace on a 10k-overdue-record
   fixture.

A **1M-node graph cannot be built in the worktree CI lane** (RAM and
wall-clock).  The benchmark suite splits in two:

* `benchmarks/test_bitemporal_perf.py::test_ci_*` — runs in CI on a
  10k-node fixture; asserts only the *shape* of the cost.  A
  sanity floor, not a budget.
* `benchmarks/test_bitemporal_perf.py::test_perf_lab_full_fixture_budget`
  — opt-in via `OMNISCIENCE_RUN_BITEMPORAL_PERF=1`.  Runs the 1M-node
  fixture; asserts the issue's quantitative budget.  Documented as a
  perf-lab activity to be run on dedicated hardware before the ADR
  flip ([#139](https://github.com/100rd/Omniscience/issues/139)).

This split is documented in the PR body and tracked as the perf-lab
follow-up activity.

## Running the suite

```bash
# Simulator suite only (CI default).
uv run pytest tests/property/ tests/integration/test_incident_demo_historical.py benchmarks/

# With live Neo4j+Qdrant (slower; requires Docker).
OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1 \
OMNISCIENCE_RUN_QDRANT_CONTRACT_TESTS=1 \
  uv run pytest tests/property/ tests/integration/

# Perf-lab full fixture (1M nodes; perf hardware only).
OMNISCIENCE_RUN_BITEMPORAL_PERF=1 \
  uv run pytest benchmarks/
```

## Cross-workspace isolation — P0 emphasis

`test_cross_workspace_isolation_under_all_as_of` is the **single most
important property** of this entire epic.  It is the integration-level
guarantee that #117/#119/#170/#171/#173/#174 collectively delivered:
no `as_of` value, no overlapping name, no shared stable_id, no edge,
no version, can leak across workspace boundaries.  Any failure here
blocks the ADR flip per the issue's acceptance criteria.

The fixture deliberately picks entity names from a small alphabet and
fixed workspace UUIDs so that the *same* `entity_key` is generated for
both workspaces in a meaningful fraction of examples — the ACL
assertion fires on the overlap.
