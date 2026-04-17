"""SQLAlchemy WHERE-clause builders for SearchRequest filters."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from omniscience_core.db.models import Chunk, Document, Source
from sqlalchemy import ColumnElement, and_, or_
from sqlalchemy.dialects.postgresql import JSONB

from .models import SearchRequest


def build_source_name_filter(sources: list[str]) -> ColumnElement[bool]:
    """Return a clause restricting results to named sources."""
    return or_(*[Source.name == name for name in sources])


def build_source_type_filter(types: list[str]) -> ColumnElement[bool]:
    """Return a clause restricting results to given source types."""
    return or_(*[Source.type == t for t in types])


def build_freshness_filter(max_age_seconds: int) -> ColumnElement[bool]:
    """Return a clause keeping only chunks indexed within max_age_seconds."""
    cutoff = datetime.now(tz=UTC) - timedelta(seconds=max_age_seconds)
    return Document.indexed_at >= cutoff


def build_tombstone_filter(include_tombstoned: bool) -> ColumnElement[bool] | None:
    """Return a clause excluding tombstoned documents unless requested."""
    if include_tombstoned:
        return None
    return Document.tombstoned_at.is_(None)


def build_metadata_filter(filters: dict[str, object]) -> ColumnElement[bool]:
    """Return a JSONB containment clause for metadata key/value filters."""
    return Chunk.chunk_metadata.cast(JSONB).contains(filters)  # type: ignore[no-any-return]


def build_where_clauses(request: SearchRequest) -> list[ColumnElement[bool]]:
    """Assemble all applicable WHERE clauses from a SearchRequest.

    The caller is responsible for joining Source and Document before applying
    these clauses.
    """
    clauses: list[ColumnElement[bool]] = []

    if request.sources:
        clauses.append(build_source_name_filter(request.sources))

    if request.types:
        clauses.append(build_source_type_filter(request.types))

    if request.max_age_seconds is not None:
        clauses.append(build_freshness_filter(request.max_age_seconds))

    tombstone_clause = build_tombstone_filter(request.include_tombstoned)
    if tombstone_clause is not None:
        clauses.append(tombstone_clause)

    if request.filters:
        clauses.append(build_metadata_filter(request.filters))

    return clauses


def combine_clauses(clauses: list[ColumnElement[bool]]) -> ColumnElement[bool] | None:
    """AND all clauses together, or return None if the list is empty."""
    if not clauses:
        return None
    return and_(*clauses)


__all__ = [
    "build_freshness_filter",
    "build_metadata_filter",
    "build_source_name_filter",
    "build_source_type_filter",
    "build_tombstone_filter",
    "build_where_clauses",
    "combine_clauses",
]
