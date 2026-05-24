# MCP retrieval-quality benchmark

Public, reproducible benchmark suite for incident-response MCP servers.
Defends the "best for SRE" claim with numbers — Omniscience vs
HolmesGPT vs Sourcegraph MCP vs vanilla GitHub MCP on a canned 50-incident
corpus.

Tracks issue [#241](https://github.com/100rd/Omniscience/issues/241)
(Wave 1, Track 4 of the benchmark + compliance epic).

## What's in this PR (scaffolding subset)

This PR lands the *plumbing* for the full benchmark — corpus structure,
runner skeleton, scoring functions, and the first 12 incident fixtures.
The remaining ~38 fixtures, the cross-vendor adapter matrix, the
published Q2 results, and the CI integration land in the follow-up PR
once Wave 1 features (#234, #235, #243) are stable enough to measure.

```
bench/
├── README.md                          ← you are here
├── __init__.py
├── schema.py                          ← dataclass schema + validator
├── scorers.py                         ← top-1, MRR, Brier, latency
├── runner.py                          ← CLI runner + Omniscience HTTP adapter
└── incidents/
    ├── bench-0001-oom-memlimit-reduction.yaml
    ├── bench-0002-oom-leak-after-cache-change.yaml
    ├── bench-0003-cert-expiry-internal-ca.yaml
    ├── bench-0004-cert-expiry-public-tls.yaml
    ├── bench-0005-dns-coredns-config-typo.yaml
    ├── bench-0006-dns-service-renamed.yaml
    ├── bench-0007-deploy-rollback-image-tag.yaml
    ├── bench-0008-deploy-rollback-helm-values.yaml
    ├── bench-0009-config-drift-feature-flag.yaml
    ├── bench-0010-dependency-outage-upstream-api.yaml
    ├── bench-0011-noisy-neighbor-cpu-throttle.yaml
    └── bench-0012-data-corruption-dual-write.yaml
```

Topical breakdown of the 12 scaffolding fixtures:

| category            | count | notes                                                |
| ------------------- | :---: | ---------------------------------------------------- |
| oom                 |   2   | memlimit reduction vs slow leak                      |
| cert-expiry         |   2   | internal mTLS vs public TLS                          |
| dns                 |   2   | CoreDNS Corefile typo vs renamed Service             |
| deploy-rollback     |   2   | bad image tag vs Helm-values HPA cut                 |
| config-drift        |   1   | feature-flag manual override lost after restart      |
| dependency-outage   |   1   | upstream payment processor degraded                  |
| noisy-neighbor      |   1   | missing PodAntiAffinity, batch starves api           |
| data-corruption     |   1   | dual-write back-fill silently exited after env rename|

`data-corruption`, `noisy-neighbor`, `dependency-outage`, and
`config-drift` will each grow to 4-6 fixtures in the follow-up PR.
The `runner.py` `--validate-corpus` mode warns about uncovered
categories.

## How to use the runner today

```bash
# 1. Validate every fixture against the schema (no server needed):
python bench/runner.py --validate-corpus

# 2. Dry-run — exercises load/score path with a stub client:
python bench/runner.py --mcp-endpoint http://localhost:8000 --dry-run

# 3. Smoke run against a live Omniscience MCP server:
docker compose up -d
python bench/runner.py --mcp-endpoint http://localhost:8000

# 4. Machine-readable output for downstream tooling:
python bench/runner.py --mcp-endpoint http://localhost:8000 --json
```

## Fixture schema

Each `bench/incidents/*.yaml` is one labelled incident:

```yaml
id: bench-XXXX
category: oom | cert-expiry | dns | deploy-rollback | config-drift
        | dependency-outage | noisy-neighbor | data-corruption
description: one-paragraph human prose
alert_payload:                     # opaque dict, handed verbatim to the MCP tool
  provider: pagerduty | prometheus | argocd | sentry | ...
  alert_id: ...
  ...
expected_root_cause:
  summary: free-text golden
  canonical_entities:              # ranked, index 0 is the top-1 target
    - "https://github.com/acme/foo/pull/123"
    - "deployment/foo"
expected_runbook_step: "single-line remediation"
expected_blast_radius:             # set of resources expected to be affected
  - "service/foo"
notes: optional
source_files: optional             # other repo paths the fixture is drawn from
```

Validation is enforced by `bench/schema.py:parse_incident`. Run
`python bench/runner.py --validate-corpus` after editing any fixture.

## Metrics

| metric              | formula                                                              | direction |
| ------------------- | -------------------------------------------------------------------- | --------- |
| top-1 accuracy      | `mean( retrieved[0] == golden_top1 )`                                | higher    |
| MRR                 | `mean( 1/rank_of_first_golden_match  or  0 )`                        | higher    |
| Brier score         | `mean( (confidence - 1{top-1 hit})^2 )`                              | lower     |
| p50 / p99 latency   | nearest-rank percentile over per-fixture wall-clock                  | lower     |

Errored calls count as RR=0 and top-1 miss. Errored calls are excluded
from the Brier score (no confidence to score) and from latency
percentiles (a network failure isn't a latency signal).

## Follow-up PR scope

This PR is ~1 of the 3 engineer-weeks the issue estimates. The
follow-up will deliver:

1. **~38 more fixtures** to reach the 50-incident target. Coverage
   targets per category: oom 8, cert-expiry 6, dns 6, deploy-rollback
   8, config-drift 6, dependency-outage 6, noisy-neighbor 5,
   data-corruption 5.
2. **Cross-vendor adapter matrix** — implementations of `MCPClient` for:
   - HolmesGPT (`bench/vendors/holmesgpt.py`)
   - Sourcegraph MCP (`bench/vendors/sourcegraph.py`)
   - vanilla GitHub MCP (`bench/vendors/github.py`)
   Each adapter normalises the vendor's flat-hits output into the same
   `MCPResponse` shape so the existing scorers apply unchanged.
3. **Published results** under `bench/results/2026-Q2.md` with a
   leaderboard table that we also surface in the top-level `README.md`.
4. **CI integration** via `.github/workflows/benchmark.yml`. Two modes:
   - per-PR: runs against a Compose-spun Omniscience only, gates on a
     top-1 regression threshold.
   - nightly: runs the full vendor matrix and publishes a JSON artifact.
5. **Blog-post draft / design-partner one-pager** summarising the
   numbers — out of engineering scope, owned by product/marketing.

## Reproducibility

The scoring code is pure-Python with no external services. Every
fixture is checked into the repo. The runner is deterministic given a
deterministic server: the same corpus + same Omniscience build + same
backing graph produces the same numbers.

The follow-up PR will pin vendor versions (HolmesGPT image digest,
Sourcegraph MCP commit SHA, GitHub MCP server commit SHA) in
`bench/vendors/versions.json` so historical results stay comparable.

