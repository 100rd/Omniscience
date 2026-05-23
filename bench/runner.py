"""Benchmark runner skeleton (issue #241, scaffolding subset).

Usage::

    # Validate the corpus without running anything:
    python bench/runner.py --validate-corpus

    # Smoke-run against a live Omniscience MCP endpoint:
    python bench/runner.py --mcp-endpoint http://localhost:8000

    # Dry-run (no network) — exercises the load/score path with mocked
    # responses, used in unit tests and as a smoke check on CI machines
    # without a live server:
    python bench/runner.py --mcp-endpoint http://localhost:8000 --dry-run

The runner is deliberately a *skeleton* in this PR.  The HTTP client
is wired enough to call ``resolve_incident`` against a live server, but
the cross-vendor adapters (HolmesGPT, Sourcegraph MCP, GitHub MCP) land
in the follow-up PR — see ``bench/README.md``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol

# Allow `python bench/runner.py ...` to work in addition to
# `python -m bench.runner ...`.  When invoked as a script, __package__
# is empty and the sibling `bench.schema` import would fail.  We patch
# sys.path to include the repo root in that case.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from bench.schema import (
    CATEGORIES,
    IncidentFixture,
    InvalidIncidentError,
    parse_incident,
)
from bench.scorers import CorpusMetrics, FixtureResult, summarise

LOG = logging.getLogger("bench.runner")

#: Default location of the corpus relative to the repo root.
DEFAULT_CORPUS_DIR = Path(__file__).resolve().parent / "incidents"

#: Default HTTP timeout, seconds.  Generous: a slow server is part of
#: what the benchmark is measuring — but a truly hung call shouldn't
#: block the whole run.
DEFAULT_TIMEOUT_SECONDS = 30.0


# ---------------------------------------------------------------------------
# Corpus loading
# ---------------------------------------------------------------------------


def load_corpus(corpus_dir: Path = DEFAULT_CORPUS_DIR) -> list[IncidentFixture]:
    """Load every ``*.yaml`` file in ``corpus_dir`` as an :class:`IncidentFixture`.

    Raises :class:`InvalidIncidentError` on the first invalid fixture so
    a corrupted corpus fails the run loudly rather than silently
    distorting metrics.
    """
    if not corpus_dir.is_dir():
        raise FileNotFoundError(f"corpus directory not found: {corpus_dir}")

    fixtures: list[IncidentFixture] = []
    seen_ids: set[str] = set()
    for path in sorted(corpus_dir.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise InvalidIncidentError(
                f"{path}: top-level YAML must be a mapping, got {type(raw).__name__}"
            )
        fixture = parse_incident(raw, path_hint=str(path))
        if fixture.id in seen_ids:
            raise InvalidIncidentError(f"{path}: duplicate fixture id {fixture.id!r}")
        seen_ids.add(fixture.id)
        fixtures.append(fixture)
    return fixtures


# ---------------------------------------------------------------------------
# MCP client adapter — protocol-shaped so tests can substitute fakes.
# ---------------------------------------------------------------------------


class MCPClient(Protocol):
    """Minimal interface the runner needs from any MCP client.

    Vendor-specific adapters (HolmesGPT, Sourcegraph, GitHub) will
    implement this in the follow-up PR.
    """

    def resolve_incident(
        self, alert_payload: dict[str, Any], *, timeout: float
    ) -> MCPResponse: ...


class MCPResponse:
    """Normalised response shape every adapter must return.

    Carries a *ranked* list of canonical entity names (the IR-style
    output the scorer needs) plus the server's self-reported
    ``confidence`` score for the top result.
    """

    __slots__ = ("confidence", "retrieved_entities")

    def __init__(
        self,
        retrieved_entities: Iterable[str],
        confidence: float,
    ) -> None:
        self.retrieved_entities: tuple[str, ...] = tuple(retrieved_entities)
        self.confidence: float = float(confidence)


class OmniscienceHTTPClient:
    """Adapter that calls a live Omniscience MCP server over HTTP.

    Uses ``httpx`` (already a dev dependency).  The exact wire format
    of the v0.1 ``resolve_incident`` MCP tool is in
    ``apps/server/src/omniscience_server/incidents.py``; the adapter
    flattens the returned bundle into the IR-style
    ``retrieved_entities`` shape expected by the scorer:

        rank 1: target_resource.name (if present)
        rank 2: responsible_pr.name (if present)
        rank 3+: slack_threads[].name in their bundle order

    This ordering reflects the v0.1 demo's own ranking opinion (target
    -> responsible PR -> discussion threads).  Vendors with a flat
    "hits" list will report it directly.
    """

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint.rstrip("/")

    def resolve_incident(self, alert_payload: dict[str, Any], *, timeout: float) -> MCPResponse:
        import httpx  # imported lazily so --validate-corpus stays dependency-light

        url = f"{self.endpoint}/mcp/tools/resolve_incident"
        response = httpx.post(url, json=alert_payload, timeout=timeout)
        response.raise_for_status()
        bundle = response.json()
        return _bundle_to_response(bundle)


def _bundle_to_response(bundle: dict[str, Any]) -> MCPResponse:
    """Flatten a ``resolve_incident`` bundle into the IR shape."""
    ranked: list[str] = []
    target = bundle.get("target_resource")
    if isinstance(target, dict) and "name" in target:
        ranked.append(str(target["name"]))
    pr = bundle.get("responsible_pr")
    if isinstance(pr, dict) and "name" in pr:
        ranked.append(str(pr["name"]))
    for thread in bundle.get("slack_threads", []) or []:
        if isinstance(thread, dict) and "name" in thread:
            ranked.append(str(thread["name"]))
    confidence = bundle.get("confidence_score", 0.0)
    return MCPResponse(retrieved_entities=ranked, confidence=float(confidence))


# ---------------------------------------------------------------------------
# Dry-run client — used by CI smoke and unit tests
# ---------------------------------------------------------------------------


class DryRunClient:
    """Returns the golden top-1 every time with confidence 1.0.

    Lets ``--dry-run`` exercise the full load/score path without a
    network server (and produces a "perfect" leaderboard, which is the
    point — it lets ops verify the runner itself is wired correctly
    before debating any actual model's quality).
    """

    def __init__(self, fixtures_by_payload: dict[str, IncidentFixture]) -> None:
        self._by_payload = fixtures_by_payload

    def resolve_incident(self, alert_payload: dict[str, Any], *, timeout: float) -> MCPResponse:
        key = json.dumps(alert_payload, sort_keys=True)
        fixture = self._by_payload[key]
        return MCPResponse(
            retrieved_entities=fixture.expected_root_cause.canonical_entities,
            confidence=1.0,
        )


# ---------------------------------------------------------------------------
# Run loop
# ---------------------------------------------------------------------------


def run_corpus(
    fixtures: list[IncidentFixture],
    client: MCPClient,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[FixtureResult]:
    """Run every fixture through the client, collecting raw results."""
    results: list[FixtureResult] = []
    for fixture in fixtures:
        start = time.perf_counter()
        error: str | None = None
        retrieved: tuple[str, ...] = ()
        confidence = 0.0
        try:
            response = client.resolve_incident(fixture.alert_payload, timeout=timeout)
            retrieved = response.retrieved_entities
            confidence = response.confidence
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            LOG.warning("fixture %s failed: %s", fixture.id, error)
        elapsed = time.perf_counter() - start
        results.append(
            FixtureResult(
                fixture_id=fixture.id,
                category=fixture.category,
                retrieved_entities=retrieved,
                confidence=confidence,
                latency_seconds=elapsed,
                golden_top1=fixture.expected_root_cause.canonical_entities[0],
                golden_all=fixture.expected_root_cause.canonical_entities,
                error=error,
            )
        )
    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def format_report(metrics: CorpusMetrics) -> str:
    """Human-readable report — one screen, ready for CI logs or a PR body."""
    lines = [
        "MCP retrieval-quality benchmark — summary",
        "=" * 45,
        f"  fixtures run    : {metrics.n}",
        f"  errored calls   : {metrics.n_errors}",
        f"  top-1 accuracy  : {metrics.top1_accuracy:.3f}",
        f"  MRR             : {metrics.mrr:.3f}",
        f"  Brier score     : {metrics.brier:.4f}   (lower is better)",
        f"  p50 latency (s) : {metrics.p50_latency_seconds:.4f}",
        f"  p99 latency (s) : {metrics.p99_latency_seconds:.4f}",
        "",
        "  per-category breakdown:",
    ]
    for cat, cat_metrics in metrics.per_category.items():
        lines.append(
            f"    {cat:<20} n={cat_metrics.n:<3} "
            f"top1={cat_metrics.top1_accuracy:.3f} mrr={cat_metrics.mrr:.3f}"
        )
    return "\n".join(lines)


def metrics_to_json(metrics: CorpusMetrics) -> str:
    """Machine-readable JSON dump of the metrics."""
    payload = asdict(metrics)
    # asdict turns CategoryMetrics into nested dicts already.
    return json.dumps(payload, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bench/runner.py",
        description=(
            "Run the MCP retrieval-quality benchmark corpus against a server. "
            "Scaffolding subset of issue #241."
        ),
    )
    parser.add_argument(
        "--mcp-endpoint",
        default=None,
        help="Base URL of the MCP server (e.g. http://localhost:8000). "
        "Required unless --validate-corpus is set.",
    )
    parser.add_argument(
        "--corpus-dir",
        type=Path,
        default=DEFAULT_CORPUS_DIR,
        help=f"Directory of incident YAMLs (default: {DEFAULT_CORPUS_DIR}).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Per-call HTTP timeout, seconds (default: {DEFAULT_TIMEOUT_SECONDS}).",
    )
    parser.add_argument(
        "--validate-corpus",
        action="store_true",
        help="Load and validate every fixture, then exit. No server needed.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Exercise the runner with a stub client that returns goldens. "
        "Useful as a CI smoke when no live server is available.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON metrics on stdout instead of the human-readable report.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s: %(message)s")

    fixtures = load_corpus(args.corpus_dir)
    LOG.info("loaded %d fixtures from %s", len(fixtures), args.corpus_dir)
    _warn_on_uncovered_categories(fixtures)

    if args.validate_corpus:
        print(f"OK: {len(fixtures)} fixtures validated.")
        return 0

    if args.mcp_endpoint is None:
        parser.error("--mcp-endpoint is required unless --validate-corpus is set")

    client: MCPClient
    if args.dry_run:
        client = DryRunClient({json.dumps(f.alert_payload, sort_keys=True): f for f in fixtures})
    else:
        client = OmniscienceHTTPClient(args.mcp_endpoint)

    results = run_corpus(fixtures, client, timeout=args.timeout)
    metrics = summarise(results)

    if args.json:
        print(metrics_to_json(metrics))
    else:
        print(format_report(metrics))
    return 0


def _warn_on_uncovered_categories(fixtures: list[IncidentFixture]) -> None:
    """Log a warning for any declared category with zero fixtures.

    Sparse categories are expected during scaffolding (this PR covers
    only ~12 fixtures across ~8 categories) but the warning keeps the
    follow-up PR honest.
    """
    covered = {f.category for f in fixtures}
    missing = sorted(set(CATEGORIES) - covered)
    if missing:
        LOG.warning("categories with no fixtures yet: %s", ", ".join(missing))


if __name__ == "__main__":
    sys.exit(main())
