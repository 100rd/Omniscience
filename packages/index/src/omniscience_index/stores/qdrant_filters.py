"""Typed filter-builder for the Qdrant vector-store adapter.

Rationale (ADR-0006 §ACL carry-forward, §Consequences §team)
-----------------------------------------------------------

The ADR mandates that every read against Qdrant apply a mandatory
``must`` clause on ``workspace_id``.  Raw construction of
``qdrant_client.models.Filter`` outside this module is a
review-rejected pattern and enforced by a lint rule (see
``tests/test_qdrant_filter_builder_lint.py``).  This module is the
ONLY place in the codebase that imports ``Filter`` / ``FieldCondition``
/ ``MatchValue`` directly.

Construction model
------------------

- ``QdrantFilterBuilder(workspace_id=...)`` takes ``workspace_id``
  as a **required** constructor argument.  There is no way to
  instantiate the builder without it.
- ``.with_source_ids([...])``, ``.with_document_ids([...])``,
  ``.exclude_tombstoned()`` etc. return a new builder (immutable
  pattern) so callers can compose safely.
- ``.build()`` emits a ``qdrant_client.models.Filter`` with
  ``workspace_id`` always present in ``must``.  A missing workspace
  is impossible by construction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace

from qdrant_client import models as qm

from omniscience_index.stores.qdrant_constants import (
    PAYLOAD_DOCUMENT_ID,
    PAYLOAD_EXTERNAL_ID,
    PAYLOAD_RECORDED_AT,
    PAYLOAD_SNAPSHOT_DATE,
    PAYLOAD_SOURCE_ID,
    PAYLOAD_TIER,
    PAYLOAD_TOMBSTONED_AT,
    PAYLOAD_WORKSPACE_ID,
)


@dataclass(frozen=True, slots=True)
class QdrantFilterBuilder:
    """Immutable builder for a workspace-scoped Qdrant filter.

    Every instance is born workspace-scoped; subsequent ``with_*``
    calls narrow the filter further but can NEVER widen the workspace
    boundary.  This class is the only sanctioned constructor of
    ``qdrant_client.models.Filter`` in the codebase.
    """

    workspace_id: uuid.UUID
    source_ids: tuple[uuid.UUID, ...] = field(default_factory=tuple)
    document_ids: tuple[uuid.UUID, ...] = field(default_factory=tuple)
    external_id: str | None = None
    exclude_tombstoned_flag: bool = False

    def with_source_ids(self, source_ids: list[uuid.UUID]) -> QdrantFilterBuilder:
        """Return a new builder narrowed to the given sources."""
        return replace(self, source_ids=tuple(source_ids))

    def with_document_ids(self, document_ids: list[uuid.UUID]) -> QdrantFilterBuilder:
        """Return a new builder narrowed to the given documents."""
        return replace(self, document_ids=tuple(document_ids))

    def with_external_id(self, external_id: str) -> QdrantFilterBuilder:
        """Return a new builder narrowed to a single external document id."""
        return replace(self, external_id=external_id)

    def exclude_tombstoned(self) -> QdrantFilterBuilder:
        """Return a new builder that excludes tombstoned points."""
        return replace(self, exclude_tombstoned_flag=True)

    def build(self) -> qm.Filter:
        """Emit the concrete Qdrant filter.

        Post-condition: ``workspace_id`` is always present in the
        ``must`` clause.  This invariant is re-asserted in a unit
        test to guard against future refactors.
        """
        # Type annotated as the full union Qdrant accepts so mypy
        # --strict does not reject list invariance.
        must: list[
            qm.FieldCondition
            | qm.IsEmptyCondition
            | qm.IsNullCondition
            | qm.HasIdCondition
            | qm.HasVectorCondition
            | qm.NestedCondition
            | qm.Filter
        ] = [
            qm.FieldCondition(
                key=PAYLOAD_WORKSPACE_ID,
                match=qm.MatchValue(value=str(self.workspace_id)),
            )
        ]
        if self.source_ids:
            must.append(
                qm.FieldCondition(
                    key=PAYLOAD_SOURCE_ID,
                    match=qm.MatchAny(any=[str(s) for s in self.source_ids]),
                )
            )
        if self.document_ids:
            must.append(
                qm.FieldCondition(
                    key=PAYLOAD_DOCUMENT_ID,
                    match=qm.MatchAny(any=[str(d) for d in self.document_ids]),
                )
            )
        if self.external_id is not None:
            must.append(
                qm.FieldCondition(
                    key=PAYLOAD_EXTERNAL_ID,
                    match=qm.MatchValue(value=self.external_id),
                )
            )
        if self.exclude_tombstoned_flag:
            # Non-tombstoned points have tombstoned_at == null; this
            # clause keeps the selection tight rather than relying on
            # must_not/not-matching semantics.
            must.append(qm.IsNullCondition(is_null=qm.PayloadField(key=PAYLOAD_TOMBSTONED_AT)))
        return qm.Filter(must=must)


def build_retention_eligible_filter(
    *,
    workspace_id: uuid.UUID,
    cutoff_iso: str,
) -> qm.Filter:
    """Build the per-workspace filter for hot-to-warm chunk eviction.

    ADR-0009 §5: chunks with ``recorded_at`` past the hot/warm boundary
    are evicted from Qdrant in the same retention run that handles the
    graph.  The filter is workspace-scoped — cross-tenant batches are
    forbidden by the same ACL invariant ADR-0006 §ACL carry-forward
    enforces on every other read path.

    The retention worker pulls eligible point ids using this filter,
    sets ``tier=warm`` + ``snapshot_date`` payloads on them, and then
    moves to the warm-to-archive band where the points are deleted
    entirely (re-embedding from archive is not supported in v1, see
    ADR-0009 §5).
    """
    must: list[
        qm.FieldCondition
        | qm.IsEmptyCondition
        | qm.IsNullCondition
        | qm.HasIdCondition
        | qm.HasVectorCondition
        | qm.NestedCondition
        | qm.Filter
    ] = [
        qm.FieldCondition(
            key=PAYLOAD_WORKSPACE_ID,
            match=qm.MatchValue(value=str(workspace_id)),
        ),
        qm.FieldCondition(
            key=PAYLOAD_RECORDED_AT,
            range=qm.DatetimeRange(lt=cutoff_iso),  # type: ignore[arg-type]
        ),
    ]
    return qm.Filter(must=must)


def build_warm_tier_filter(*, workspace_id: uuid.UUID) -> qm.Filter:
    """Build a workspace-scoped filter for ALL warm-tier chunks.

    ADR-0009 §5: payload-indexed ``tier`` field marks warm chunks.
    Used by the metrics/stats path to roll up warm-tier counts without
    pinning a specific ``snapshot_date``.  Workspace-scoped — the
    cross-tenant invariant from ADR-0006 §ACL carry-forward applies.
    """
    must: list[
        qm.FieldCondition
        | qm.IsEmptyCondition
        | qm.IsNullCondition
        | qm.HasIdCondition
        | qm.HasVectorCondition
        | qm.NestedCondition
        | qm.Filter
    ] = [
        qm.FieldCondition(
            key=PAYLOAD_WORKSPACE_ID,
            match=qm.MatchValue(value=str(workspace_id)),
        ),
        qm.FieldCondition(
            key=PAYLOAD_TIER,
            match=qm.MatchValue(value="warm"),
        ),
    ]
    return qm.Filter(must=must)


def build_warm_archive_filter(
    *,
    workspace_id: uuid.UUID,
    snapshot_date_iso: str,
) -> qm.Filter:
    """Build the per-workspace filter for warm-to-archive chunk deletion.

    ADR-0009 §5 archive shape: chunks past the warm-to-archive boundary
    are evicted from Qdrant entirely; the filter selects warm-tier
    chunks for the specific snapshot date being archived.
    """
    must: list[
        qm.FieldCondition
        | qm.IsEmptyCondition
        | qm.IsNullCondition
        | qm.HasIdCondition
        | qm.HasVectorCondition
        | qm.NestedCondition
        | qm.Filter
    ] = [
        qm.FieldCondition(
            key=PAYLOAD_WORKSPACE_ID,
            match=qm.MatchValue(value=str(workspace_id)),
        ),
        qm.FieldCondition(
            key=PAYLOAD_TIER,
            match=qm.MatchValue(value="warm"),
        ),
        qm.FieldCondition(
            key=PAYLOAD_SNAPSHOT_DATE,
            match=qm.MatchValue(value=snapshot_date_iso),
        ),
    ]
    return qm.Filter(must=must)


def build_tombstone_sweep_filter(*, cutoff_iso: str) -> qm.Filter:
    """Admin-only sweep filter for global tombstone purge.

    This is the **only** read-like Qdrant filter that intentionally
    omits ``workspace_id``: it is consumed exclusively by
    ``QdrantVectorStore.delete_tombstoned``, a cron-style maintenance
    sweep that hard-deletes tombstoned points older than a cutoff
    across every tenant.  The filter is defined in this module so
    the lint rule that forbids raw ``qm.Filter(...)`` elsewhere in
    the codebase (ADR-0006 §ACL carry-forward) still holds.
    """
    return qm.Filter(
        must=[
            qm.FieldCondition(
                key=PAYLOAD_TOMBSTONED_AT,
                range=qm.DatetimeRange(lt=cutoff_iso),  # type: ignore[arg-type]
            )
        ]
    )


__all__ = [
    "QdrantFilterBuilder",
    "build_retention_eligible_filter",
    "build_tombstone_sweep_filter",
    "build_warm_archive_filter",
    "build_warm_tier_filter",
]
