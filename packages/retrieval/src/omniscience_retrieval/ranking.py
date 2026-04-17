"""Reciprocal Rank Fusion for merging multiple ranked result lists."""

from __future__ import annotations

import uuid
from collections import defaultdict


def reciprocal_rank_fusion(
    ranked_lists: list[list[tuple[uuid.UUID, float]]],
    k: int = 60,
) -> list[tuple[uuid.UUID, float]]:
    """Merge multiple ranked lists using Reciprocal Rank Fusion.

    Each element in a ranked list is a (chunk_id, original_score) tuple.
    The original score is unused by RRF itself; only rank position matters.
    Returns a list of (chunk_id, rrf_score) sorted descending by rrf_score.

    Args:
        ranked_lists: One list per retrieval method.  Each inner list must be
            ordered best-first.  Items are (chunk_id, raw_score) pairs.
        k: Smoothing constant (default 60, as per original RRF paper).

    Returns:
        Merged list of (chunk_id, rrf_score) sorted descending.
    """
    scores: dict[uuid.UUID, float] = defaultdict(float)
    for ranked in ranked_lists:
        for rank, (chunk_id, _raw_score) in enumerate(ranked, start=1):
            scores[chunk_id] += 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)
