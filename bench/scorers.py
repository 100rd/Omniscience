"""Scoring functions for the MCP retrieval-quality benchmark (issue #241).

All scorers are pure functions on already-collected results — they do
not call the MCP server.  The runner collects raw per-fixture outputs
and then hands them to the scorers in bulk so that aggregate metrics
(MRR, Brier, latency percentiles) can be reported.

Metric definitions
==================

top-1 accuracy
    Fraction of fixtures where the MCP server's top-ranked retrieved
    entity equals the golden ``canonical_entities[0]``.  Strict string
    equality on the canonical name.

MRR (Mean Reciprocal Rank)
    Standard IR metric.  For each fixture, find the rank ``r`` of the
    first retrieved entity that matches *any* of the golden
    ``canonical_entities``; contribute ``1/r`` if found, ``0`` if not.
    MRR is the mean across the corpus.

    Formula::

        MRR = (1/N) * sum_i ( 1 / rank_i  if hit else 0 )

    where ``rank_i`` is 1-indexed.

calibrated-confidence Brier score
    Measures calibration of ``confidence_score`` against binary
    correctness (top-1 hit).  Lower is better; range [0, 1].

    Formula::

        Brier = (1/N) * sum_i ( confidence_i - correct_i )^2

    where ``correct_i ∈ {0, 1}`` is whether the fixture's top-1 was hit.

p50 / p99 latency
    Wall-clock seconds from "send request" to "receive parsed bundle",
    measured per fixture.  Percentiles computed with the
    nearest-rank method (no interpolation) — small corpora make
    interpolation noise dominate the signal.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FixtureResult:
    """Raw per-fixture run output, fed to the aggregate scorers."""

    fixture_id: str
    category: str
    retrieved_entities: tuple[str, ...]  # ranked, top first
    confidence: float  # MCP server's self-reported [0, 1]
    latency_seconds: float
    golden_top1: str
    golden_all: tuple[str, ...]
    error: str | None = None  # set if the call failed


@dataclass(frozen=True, slots=True)
class CorpusMetrics:
    n: int
    n_errors: int
    top1_accuracy: float
    mrr: float
    brier: float
    p50_latency_seconds: float
    p99_latency_seconds: float
    per_category: dict[str, CategoryMetrics]


@dataclass(frozen=True, slots=True)
class CategoryMetrics:
    n: int
    top1_accuracy: float
    mrr: float


def _is_top1_hit(result: FixtureResult) -> bool:
    if result.error is not None or not result.retrieved_entities:
        return False
    return result.retrieved_entities[0] == result.golden_top1


def _reciprocal_rank(result: FixtureResult) -> float:
    """Reciprocal rank of the first golden match in ``retrieved_entities``.

    Returns 0.0 if the call errored or no golden entity was retrieved.
    Ranks are 1-indexed.
    """
    if result.error is not None:
        return 0.0
    golden = set(result.golden_all)
    for idx, entity in enumerate(result.retrieved_entities, start=1):
        if entity in golden:
            return 1.0 / idx
    return 0.0


def top1_accuracy(results: Sequence[FixtureResult]) -> float:
    """Fraction of results where the top-ranked entity matches the golden top-1."""
    if not results:
        return 0.0
    hits = sum(1 for r in results if _is_top1_hit(r))
    return hits / len(results)


def mean_reciprocal_rank(results: Sequence[FixtureResult]) -> float:
    """MRR over the given results.

    Errors count as RR=0 (a failed call cannot retrieve anything).
    """
    if not results:
        return 0.0
    total = sum(_reciprocal_rank(r) for r in results)
    return total / len(results)


def brier_score(results: Sequence[FixtureResult]) -> float:
    """Brier score of ``confidence`` against binary top-1 correctness.

    Errored calls are excluded (no confidence to score).
    """
    scorable = [r for r in results if r.error is None]
    if not scorable:
        return 0.0
    total = 0.0
    for r in scorable:
        correct = 1.0 if _is_top1_hit(r) else 0.0
        diff = r.confidence - correct
        total += diff * diff
    return total / len(scorable)


def latency_percentile(results: Sequence[FixtureResult], percentile: float) -> float:
    """Nearest-rank percentile of latencies across all (non-errored) results.

    ``percentile`` is in [0, 100].  Returns 0.0 on empty input.
    """
    if not 0.0 <= percentile <= 100.0:
        raise ValueError(f"percentile must be in [0, 100]; got {percentile}")
    samples = sorted(r.latency_seconds for r in results if r.error is None)
    if not samples:
        return 0.0
    # Nearest-rank: rank = ceil(p/100 * N), 1-indexed, clamped to [1, N].
    import math

    rank = max(1, min(len(samples), math.ceil(percentile / 100.0 * len(samples))))
    return samples[rank - 1]


def summarise(results: Sequence[FixtureResult]) -> CorpusMetrics:
    """Compute the full :class:`CorpusMetrics` summary."""
    by_cat: dict[str, list[FixtureResult]] = {}
    for r in results:
        by_cat.setdefault(r.category, []).append(r)

    per_category = {
        cat: CategoryMetrics(
            n=len(items),
            top1_accuracy=top1_accuracy(items),
            mrr=mean_reciprocal_rank(items),
        )
        for cat, items in sorted(by_cat.items())
    }

    return CorpusMetrics(
        n=len(results),
        n_errors=sum(1 for r in results if r.error is not None),
        top1_accuracy=top1_accuracy(results),
        mrr=mean_reciprocal_rank(results),
        brier=brier_score(results),
        p50_latency_seconds=latency_percentile(results, 50.0),
        p99_latency_seconds=latency_percentile(results, 99.0),
        per_category=per_category,
    )
