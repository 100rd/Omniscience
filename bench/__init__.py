"""Public MCP retrieval-quality benchmark suite (issue #241).

50-incident corpus across 8 SRE categories, runner + scoring, and
cross-vendor adapters (Omniscience, HolmesGPT, Sourcegraph MCP, vanilla
GitHub MCP). Published results live under ``bench/results/``; the CI
matrix in ``.github/workflows/benchmark.yml`` runs Omniscience-only on
every PR (regression gate) and the full vendor matrix nightly.
"""
