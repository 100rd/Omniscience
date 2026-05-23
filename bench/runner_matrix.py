"""Cross-vendor matrix runner — drives the 4 adapters against the corpus.

Sibling to ``bench/runner.py``; that module owns the Omniscience-only
path (per-PR regression gate, scaffolding contract from PR #249). This
module owns the multi-vendor leaderboard (nightly CI + the published
``bench/results/2026-Q2.md`` table).

Usage::

    # Mock-mode (default; no network) — verifies the matrix wiring:
    python bench/runner_matrix.py --vendor all --json > out.json

    # Live, single vendor:
    BENCH_LIVE_VENDORS=1 python bench/runner_matrix.py --vendor omniscience

    # Live full matrix (nightly):
    BENCH_LIVE_VENDORS=1 python bench/runner_matrix.py --vendor all

Output schema (JSON)::

    {
      "as_of": "2026-05-24T00:00:00+00:00",
      "live": false,
      "results": {
        "omniscience":     { ...CorpusMetrics... },
        "holmesgpt":       { ...CorpusMetrics... },
        "sourcegraph_mcp": { ...CorpusMetrics... },
        "github_mcp":      { ...CorpusMetrics... }
      }
    }
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.runner import (
    DEFAULT_CORPUS_DIR,
    DEFAULT_TIMEOUT_SECONDS,
    MCPClient,
    load_corpus,
    run_corpus,
)
from bench.scorers import summarise
from bench.vendors import (
    GitHubMCPAdapter,
    HolmesGPTAdapter,
    OmniscienceAdapter,
    SourcegraphMCPAdapter,
)
from bench.vendors._base import is_live_mode

LOG = logging.getLogger("bench.runner_matrix")

#: Stable order so leaderboard rows render deterministically.
VENDOR_ORDER: tuple[str, ...] = (
    "omniscience",
    "holmesgpt",
    "sourcegraph_mcp",
    "github_mcp",
)


def _build_client(name: str) -> MCPClient:
    if name == "omniscience":
        return OmniscienceAdapter()
    if name == "holmesgpt":
        return HolmesGPTAdapter()
    if name == "sourcegraph_mcp":
        return SourcegraphMCPAdapter()
    if name == "github_mcp":
        return GitHubMCPAdapter()
    raise ValueError(f"unknown vendor {name!r}; choices: {VENDOR_ORDER}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bench/runner_matrix.py",
        description="Run the MCP retrieval-quality benchmark across vendors.",
    )
    parser.add_argument(
        "--vendor",
        default="all",
        help=f"Vendor to run, or 'all' (default). Choices: {(*VENDOR_ORDER, 'all')}.",
    )
    parser.add_argument(
        "--corpus-dir",
        type=Path,
        default=DEFAULT_CORPUS_DIR,
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a leaderboard table.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return parser


def _resolve_vendors(vendor_arg: str) -> tuple[str, ...]:
    if vendor_arg == "all":
        return VENDOR_ORDER
    if vendor_arg not in VENDOR_ORDER:
        raise SystemExit(f"unknown vendor {vendor_arg!r}; choices: {(*VENDOR_ORDER, 'all')}")
    return (vendor_arg,)


def format_leaderboard(payload: dict[str, object]) -> str:
    """Human-readable leaderboard. Mirrors the table style in 2026-Q2.md."""
    lines = [
        "MCP retrieval-quality benchmark — cross-vendor matrix",
        "=" * 56,
        f"  as_of : {payload['as_of']}",
        f"  live  : {payload['live']}",
        "",
        f"  {'vendor':<18} {'n':>3} {'top-1':>6} {'MRR':>6} {'Brier':>7}"
        f" {'p50(s)':>8} {'p99(s)':>8}",
        f"  {'-' * 18:<18} {'-' * 3:>3} {'-' * 6:>6} {'-' * 6:>6} {'-' * 7:>7}"
        f" {'-' * 8:>8} {'-' * 8:>8}",
    ]
    results = payload.get("results", {})
    if not isinstance(results, dict):
        raise TypeError("payload['results'] must be a dict")
    for name in VENDOR_ORDER:
        m = results.get(name)
        if not isinstance(m, dict):
            continue
        lines.append(
            f"  {name:<18} {m['n']:>3} "
            f"{m['top1_accuracy']:>6.3f} {m['mrr']:>6.3f} {m['brier']:>7.4f} "
            f"{m['p50_latency_seconds']:>8.4f} {m['p99_latency_seconds']:>8.4f}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s: %(message)s")

    fixtures = load_corpus(args.corpus_dir)
    LOG.info("loaded %d fixtures", len(fixtures))

    vendor_names = _resolve_vendors(args.vendor)
    payload: dict[str, object] = {
        "as_of": datetime.now(UTC).isoformat(),
        "live": is_live_mode(),
        "results": {},
    }
    results_dict: dict[str, object] = {}
    for name in vendor_names:
        LOG.info("running vendor %s", name)
        client = _build_client(name)
        per_fixture = run_corpus(fixtures, client, timeout=args.timeout)
        metrics = summarise(per_fixture)
        results_dict[name] = asdict(metrics)
    payload["results"] = results_dict

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(format_leaderboard(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
