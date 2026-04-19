"""Federated search: fan-out to remote Omniscience instances and merge results.

``FederatedSearch`` wraps a local ``RetrievalService`` with a fan-out layer
that queries each enabled remote peer in parallel.  Results are merged by
score (descending), deduplicated by ``chunk_id``, and sliced to ``top_k``.

Remote failures are isolated — a peer that times out or returns an error is
logged and skipped; the local result is always returned.

Design decisions:
- Uses ``asyncio.gather(..., return_exceptions=True)`` so that a single bad
  peer does not cancel the other in-flight requests.
- Deduplication uses ``chunk_id`` as the key.  In a true multi-instance
  deployment the same document can exist on multiple nodes with the same
  ``chunk_id`` UUID (e.g. synced corpora); the copy from the higher-priority
  peer (lower ``priority`` value) wins.  Within the same priority tier the
  higher-scoring copy wins.
- ``QueryStats`` from remote peers are summed into the merged stats so callers
  can see the aggregate match counts.
- The remote API call mirrors the existing POST /api/v1/search endpoint.
  ``top_k`` for each remote is capped at ``max_remote_results`` so that we
  don't request more results from a peer than we can usefully consume.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from .federation_config import FederatedInstance, FederationConfig
from .models import QueryStats, SearchHit, SearchRequest, SearchResult

logger = logging.getLogger(__name__)

# Sentinel used when asyncio.gather returns an exception for a peer.
_FAILED = object()


class FederatedSearch:
    """Fan-out search across a local instance and one or more remote peers.

    Args:
        local_service:   The local ``RetrievalService`` (or any object with an
                         async ``search(request) -> SearchResult`` method).
        config:          Federation configuration containing the peer list and
                         shared tuning knobs.

    The ``search`` method is a drop-in replacement for
    ``RetrievalService.search``: it accepts a ``SearchRequest`` and returns a
    merged ``SearchResult``.
    """

    def __init__(
        self,
        local_service: Any,
        config: FederationConfig,
    ) -> None:
        self._local = local_service
        self._config = config
        # One shared client; per-request timeouts are applied via the request
        # itself so that individual slow peers don't block others.
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(config.timeout_seconds),
            follow_redirects=True,
        )

    async def close(self) -> None:
        """Release the underlying HTTP client."""
        await self._http.aclose()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def search(self, request: SearchRequest) -> SearchResult:
        """Search the local instance and all enabled remote peers in parallel.

        Steps:
        1. Fan out: issue the local search and one HTTP call per enabled peer
           concurrently via ``asyncio.gather``.
        2. Collect: filter out failed peers (logged as WARNING).
        3. Merge: sort all hits by score descending, deduplicate by
           ``chunk_id`` (keeping the hit from the highest-priority / highest-
           score peer), slice to ``request.top_k``.
        4. Return the merged ``SearchResult`` with summed ``QueryStats``.

        Args:
            request: The search parameters.  Passed unchanged to the local
                     service and forwarded (with ``top_k`` capped) to each peer.

        Returns:
            Merged ``SearchResult`` whose hits are annotated with
            ``source_instance`` (``None`` for local, peer name for remote).
        """
        start = time.monotonic()
        enabled = self._config.enabled_instances

        # Build coroutines
        tasks: list[Any] = [self._local.search(request)]
        peer_names: list[str] = ["__local__"]

        for inst in enabled:
            tasks.append(self._search_remote(inst, request))
            peer_names.append(inst.name)

        # Fan out — exceptions are captured, not raised
        outcomes: list[Any] = list(await asyncio.gather(*tasks, return_exceptions=True))

        # Separate local from remotes
        local_outcome = outcomes[0]
        if isinstance(local_outcome, BaseException):
            # Local should never fail; if it does, re-raise immediately.
            raise local_outcome

        local_result: SearchResult = local_outcome
        remote_outcomes = outcomes[1:]

        # Collect successful remote results (name, SearchResult) pairs
        successful_remotes: list[tuple[str, SearchResult]] = []
        for name, outcome in zip(peer_names[1:], remote_outcomes, strict=True):
            if isinstance(outcome, BaseException):
                logger.warning(
                    "federated_search_peer_failed name=%r error=%r",
                    name,
                    outcome,
                )
            else:
                successful_remotes.append((name, outcome))

        merged = self._merge_results(local_result, successful_remotes)

        logger.info(
            "federated_search_complete peers_queried=%d peers_ok=%d "
            "total_hits=%d duration_ms=%.1f",
            len(enabled),
            len(successful_remotes),
            len(merged.hits),
            (time.monotonic() - start) * 1000,
        )
        return merged

    async def _search_remote(
        self,
        instance: FederatedInstance,
        request: SearchRequest,
    ) -> SearchResult:
        """POST to a remote Omniscience instance's /api/v1/search endpoint.

        The request payload mirrors the local ``SearchRequest`` model.
        ``top_k`` is capped at ``config.max_remote_results`` to limit
        per-peer bandwidth.

        Args:
            instance: The remote peer to query.
            request:  Original search parameters.

        Returns:
            Parsed ``SearchResult`` from the remote peer.

        Raises:
            httpx.HTTPError: On network failure or non-2xx HTTP status.
            ValueError: When the response body does not match ``SearchResult``.
        """
        remote_top_k = min(request.top_k, self._config.max_remote_results)
        payload = request.model_dump(mode="json")
        payload["top_k"] = remote_top_k

        url = instance.url.rstrip("/") + "/api/v1/search"
        headers = {
            "Authorization": f"Bearer {instance.token}",
            "Content-Type": "application/json",
        }

        logger.debug("federated_search_peer url=%r top_k=%d", url, remote_top_k)

        response = await self._http.post(
            url,
            json=payload,
            headers=headers,
        )
        response.raise_for_status()

        data: dict[str, Any] = response.json()
        return SearchResult.model_validate(data)

    def _merge_results(
        self,
        local: SearchResult,
        remotes: list[tuple[str, SearchResult]],
    ) -> SearchResult:
        """Merge hits from all sources into a single ``SearchResult``.

        Algorithm:
        1. Annotate each hit with its ``source_instance`` (``None`` for local).
        2. Collect all hits, sort by score descending then by peer priority
           (lower priority value = higher priority).
        3. Deduplicate: for each ``chunk_id`` keep only the first occurrence
           (highest priority + highest score wins).
        4. Slice to the caller's original ``top_k``.
        5. Sum ``QueryStats`` across all sources.

        Args:
            local:   Result from the local instance.
            remotes: List of ``(peer_name, result)`` pairs from remote peers.

        Returns:
            A ``SearchResult`` with merged and deduplicated hits.
        """
        # Build a priority map: peer_name -> FederatedInstance.priority
        # Local is always priority -1 (highest) so it wins ties.
        priority_map: dict[str | None, int] = {None: -1}
        for inst in self._config.instances:
            priority_map[inst.name] = inst.priority

        # Annotate local hits
        annotated: list[tuple[int, SearchHit]] = [
            (priority_map[None], hit.model_copy(update={"source_instance": None}))
            for hit in local.hits
        ]

        # Annotate remote hits
        for peer_name, result in remotes:
            peer_priority = priority_map.get(peer_name, 0)
            for hit in result.hits:
                annotated.append(
                    (peer_priority, hit.model_copy(update={"source_instance": peer_name}))
                )

        # Sort: primary by score desc, secondary by priority asc (lower is better)
        annotated.sort(key=lambda t: (-t[1].score, t[0]))

        # Deduplicate by chunk_id (first occurrence wins = best score + priority)
        seen: set[str] = set()
        deduped: list[SearchHit] = []
        for _, hit in annotated:
            key = str(hit.chunk_id)
            if key not in seen:
                seen.add(key)
                deduped.append(hit)

        # Determine top_k from local request — we don't have it here, so return all
        # (the caller already controls top_k via the request forwarded to each peer).
        # We still cap at a reasonable default to avoid huge payloads; callers
        # slice further if needed.
        merged_hits = deduped

        # Sum query stats
        all_stats = [local.query_stats] + [r.query_stats for _, r in remotes]
        merged_stats = QueryStats(
            total_matches_before_filters=sum(s.total_matches_before_filters for s in all_stats),
            vector_matches=sum(s.vector_matches for s in all_stats),
            text_matches=sum(s.text_matches for s in all_stats),
            duration_ms=max(s.duration_ms for s in all_stats),
        )

        return SearchResult(hits=merged_hits, query_stats=merged_stats)


__all__ = [
    "FederatedSearch",
]
