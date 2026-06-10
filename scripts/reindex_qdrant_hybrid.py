#!/usr/bin/env python3
"""Reindex existing Qdrant collection to include sparse (BM25) vectors.

Stage 3 migration script (refactor/bitemporal-vector-hybrid).

PROBLEM
-------
Before Stage 3 the Qdrant collection had only a single named vector
(``dense_primary``).  After Stage 3, every new chunk upsert also stores
a sparse vector (``sparse_bm25``) in a dedicated named-vector slot.
Existing chunks ingested before Stage 3 have no ``sparse_bm25`` payload
and are therefore invisible to keyword / hybrid-RRF queries.

This script backfills those sparse vectors for all existing points that
do NOT yet have a ``sparse_bm25`` vector.

DESIGN
------
- Idempotent: points that already have ``sparse_bm25`` are skipped.
- --dry-run: counts eligible points and exits without writing.
- Reads points via scroll (page_size=256) to stay within gRPC message
  limits.
- Reads ``text`` from the payload, tokenises client-side (same
  ``_tokenize_to_sparse`` logic as ``qdrant_store.py``).
- Upserts sparse vector via ``update_vectors`` (not upsert_points) so
  the dense vector and the payload are untouched.
- Respects the bitemporal structure: end-dated points (``valid_to`` set)
  also get their sparse vector backfilled — they remain queryable via
  ``as_of`` and need keyword matches too.
- Does NOT touch the docker stack; does NOT require the full
  Omniscience server to be running — only a Qdrant endpoint.

DO NOT RUN ON PRODUCTION WITHOUT A BACKUP.

Usage
-----
    python scripts/reindex_qdrant_hybrid.py \\
        --host localhost --port 6334 \\
        --collection omniscience__text-embedding-3-small__d1536 \\
        [--dry-run] [--batch-size 64] [--verbose]

Environment variables (overridden by CLI flags if provided):
    QDRANT_HOST         Qdrant gRPC host (default: localhost)
    QDRANT_GRPC_PORT    Qdrant gRPC port (default: 6334)
    QDRANT_API_KEY      Optional API key (if Qdrant is auth-protected)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
import time
from collections import Counter as _Counter
from typing import Any

# ---------------------------------------------------------------------------
# Sparse tokenizer (must stay in sync with qdrant_store._tokenize_to_sparse)
# ---------------------------------------------------------------------------

_SPARSE_VOCAB_SIZE: int = 1 << 20  # 2^20 = 1,048,576 hash buckets

_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "in",
        "is",
        "it",
        "to",
        "be",
        "as",
        "at",
        "by",
        "for",
        "on",
        "with",
        "this",
        "that",
        "from",
        "was",
        "are",
        "has",
        "had",
        "not",
        "but",
        "have",
    }
)

NAMED_VECTOR_DENSE_PRIMARY = "dense_primary"
NAMED_VECTOR_SPARSE_BM25 = "sparse_bm25"
PAYLOAD_TEXT = "text"


def _tokenize_to_sparse(text: str) -> tuple[list[int], list[float]]:
    """Client-side TF tokeniser → sparse vector (indices, values).

    Identical to ``qdrant_store._tokenize_to_sparse`` but returns plain
    Python lists to avoid a qdrant_client import at the module level
    (the import is deferred to ``main``).

    Returns
    -------
    (indices, values) — parallel lists; values are max-TF normalised.
    Empty lists when the text contains no usable tokens.
    """
    tokens = re.split(r"[^a-z0-9]+", text.lower())
    counts: _Counter[int] = _Counter()
    for tok in tokens:
        if len(tok) <= 1 or tok in _STOPWORDS:
            continue
        idx = hash(tok) % _SPARSE_VOCAB_SIZE
        counts[idx] += 1
    if not counts:
        return [], []
    max_tf = max(counts.values())
    indices = sorted(counts)
    values = [counts[i] / max_tf for i in indices]
    return indices, values


# ---------------------------------------------------------------------------
# Main reindexer
# ---------------------------------------------------------------------------

logger = logging.getLogger("reindex_qdrant_hybrid")


async def _reindex(
    *,
    host: str,
    port: int,
    api_key: str | None,
    collection_name: str,
    dry_run: bool,
    batch_size: int,
    verbose: bool,
) -> None:
    """Core reindex loop — connects to Qdrant and backfills sparse vectors."""
    try:
        from qdrant_client import AsyncQdrantClient
        from qdrant_client import models as qm
    except ImportError as exc:
        logger.error(
            "qdrant-client is not installed. "
            "Run: pip install qdrant-client  or  uv add qdrant-client"
        )
        raise SystemExit(1) from exc

    client = AsyncQdrantClient(
        host=host,
        grpc_port=port,
        prefer_grpc=True,
        api_key=api_key,
    )
    try:
        await _do_reindex(
            client=client,
            qm=qm,
            collection_name=collection_name,
            dry_run=dry_run,
            batch_size=batch_size,
            verbose=verbose,
        )
    finally:
        await client.close()


async def _ensure_sparse_vector_config(
    client: Any,
    qm: Any,
    collection_name: str,
    dry_run: bool,
) -> None:
    """Ensure the collection has a ``sparse_bm25`` named-vector slot.

    If the slot is already present this is a no-op (idempotent).
    Qdrant's ``update_collection`` with ``sparse_vectors_config`` only
    adds missing named-vector slots — it does not overwrite existing ones.
    """
    info = await client.get_collection(collection_name)
    existing_sparse: dict[str, Any] = {}
    if info.config and info.config.params:
        sv = getattr(info.config.params, "sparse_vectors_config", None) or {}
        existing_sparse = dict(sv)

    if NAMED_VECTOR_SPARSE_BM25 in existing_sparse:
        logger.info("sparse_bm25 named-vector slot already exists — schema OK")
        return

    if dry_run:
        logger.info(
            "[DRY-RUN] Would add sparse_bm25 named-vector slot to collection %s",
            collection_name,
        )
        return

    logger.info("Adding sparse_bm25 named-vector slot to collection %s …", collection_name)
    await client.update_collection(
        collection_name=collection_name,
        sparse_vectors_config={
            NAMED_VECTOR_SPARSE_BM25: qm.SparseVectorParams(
                index=qm.SparseIndexParams(on_disk=False)
            )
        },
    )
    logger.info("sparse_bm25 slot created.")


async def _do_reindex(
    *,
    client: Any,
    qm: Any,
    collection_name: str,
    dry_run: bool,
    batch_size: int,
    verbose: bool,
) -> None:
    """Scroll through the collection and backfill ``sparse_bm25`` vectors."""
    await _ensure_sparse_vector_config(
        client=client,
        qm=qm,
        collection_name=collection_name,
        dry_run=dry_run,
    )

    total_scanned = 0
    total_skipped = 0  # already have sparse vector
    total_empty_text = 0  # text payload missing
    total_written = 0
    total_empty_sparse = 0  # text produced no tokens

    offset: Any = None
    page_size = 256
    batch_ids: list[str] = []
    batch_sparse: list[tuple[list[int], list[float]]] = []

    start = time.monotonic()

    while True:
        records, next_offset = await client.scroll(
            collection_name=collection_name,
            scroll_filter=None,
            limit=page_size,
            with_payload=True,
            with_vectors=[NAMED_VECTOR_SPARSE_BM25],  # only fetch sparse slot
            offset=offset,
        )

        for rec in records:
            total_scanned += 1

            # Check if sparse vector already present (non-empty)
            existing_vectors = rec.vector or {}
            sparse_vec = existing_vectors.get(NAMED_VECTOR_SPARSE_BM25)
            if sparse_vec is not None and (
                getattr(sparse_vec, "indices", None) or isinstance(sparse_vec, list)
            ):
                total_skipped += 1
                if verbose:
                    logger.debug("SKIP %s — already has sparse_bm25", rec.id)
                continue

            payload = rec.payload or {}
            text: str = payload.get(PAYLOAD_TEXT, "")
            if not text:
                total_empty_text += 1
                if verbose:
                    logger.debug("SKIP %s — empty text payload", rec.id)
                continue

            indices, values = _tokenize_to_sparse(text)
            if not indices:
                total_empty_sparse += 1
                if verbose:
                    logger.debug("SKIP %s — no sparse tokens for text %r…", rec.id, text[:40])
                continue

            batch_ids.append(str(rec.id))
            batch_sparse.append((indices, values))

            if len(batch_ids) >= batch_size:
                total_written += await _flush_batch(
                    client=client,
                    qm=qm,
                    collection_name=collection_name,
                    batch_ids=batch_ids,
                    batch_sparse=batch_sparse,
                    dry_run=dry_run,
                    verbose=verbose,
                )
                batch_ids = []
                batch_sparse = []

        if next_offset is None:
            break
        offset = next_offset

    # Flush the tail batch
    if batch_ids:
        total_written += await _flush_batch(
            client=client,
            qm=qm,
            collection_name=collection_name,
            batch_ids=batch_ids,
            batch_sparse=batch_sparse,
            dry_run=dry_run,
            verbose=verbose,
        )

    elapsed = time.monotonic() - start
    action = "[DRY-RUN]" if dry_run else "DONE"
    logger.info(
        "%s  scanned=%d  skipped=%d  empty_text=%d  empty_sparse=%d  written=%d  elapsed=%.1fs",
        action,
        total_scanned,
        total_skipped,
        total_empty_text,
        total_empty_sparse,
        total_written,
        elapsed,
    )


async def _flush_batch(
    *,
    client: Any,
    qm: Any,
    collection_name: str,
    batch_ids: list[str],
    batch_sparse: list[tuple[list[int], list[float]]],
    dry_run: bool,
    verbose: bool,
) -> int:
    """Write a batch of sparse-vector updates to Qdrant.

    Returns the number of points written (0 in dry-run mode).

    Uses ``update_vectors`` (not ``upsert_points``) so the dense vector
    and payload remain untouched — only the ``sparse_bm25`` slot is set.
    """
    if dry_run:
        if verbose:
            logger.debug("[DRY-RUN] Would update %d points: %s …", len(batch_ids), batch_ids[:3])
        return 0

    points = [
        qm.PointVectors(
            id=pid,
            vector={
                NAMED_VECTOR_SPARSE_BM25: qm.SparseVector(
                    indices=indices,
                    values=values,
                )
            },
        )
        for pid, (indices, values) in zip(batch_ids, batch_sparse, strict=True)
    ]

    await client.update_vectors(
        collection_name=collection_name,
        points=points,
        wait=True,
    )
    if verbose:
        logger.debug("Updated %d points", len(points))
    return len(points)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill sparse (BM25) vectors for existing Qdrant points. "
            "Stage 3 migration — refactor/bitemporal-vector-hybrid. "
            "DO NOT RUN ON PRODUCTION WITHOUT A BACKUP."
        )
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("QDRANT_HOST", "localhost"),
        help="Qdrant gRPC host (default: QDRANT_HOST env / localhost)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("QDRANT_GRPC_PORT", "6334")),
        help="Qdrant gRPC port (default: QDRANT_GRPC_PORT env / 6334)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("QDRANT_API_KEY"),
        help="Qdrant API key (default: QDRANT_API_KEY env, omit if unauthenticated)",
    )
    parser.add_argument(
        "--collection",
        required=True,
        help=(
            "Exact collection name to reindex (e.g. omniscience__text-embedding-3-small__d1536)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count eligible points and print summary without writing.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=64,
        help="Number of points per update_vectors call (default: 64)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Emit per-point DEBUG logging.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    if not args.dry_run:
        logger.warning(
            "LIVE RUN — writing sparse vectors to collection '%s' on %s:%d. "
            "Press Ctrl+C within 3 seconds to abort.",
            args.collection,
            args.host,
            args.port,
        )
        try:
            time.sleep(3)
        except KeyboardInterrupt:
            logger.info("Aborted by user.")
            sys.exit(0)

    asyncio.run(
        _reindex(
            host=args.host,
            port=args.port,
            api_key=args.api_key,
            collection_name=args.collection,
            dry_run=args.dry_run,
            batch_size=args.batch_size,
            verbose=args.verbose,
        )
    )


if __name__ == "__main__":
    main()
