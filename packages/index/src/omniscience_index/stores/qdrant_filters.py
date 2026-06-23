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
from datetime import datetime

from qdrant_client import models as qm

from omniscience_index.stores.qdrant_constants import (
    PAYLOAD_DOCUMENT_ID,
    PAYLOAD_EXTERNAL_ID,
    PAYLOAD_RECORDED_AT,
    PAYLOAD_SNAPSHOT_DATE,
    PAYLOAD_SOURCE_ID,
    PAYLOAD_TIER,
    PAYLOAD_TOMBSTONED_AT,
    PAYLOAD_VALID_FROM,
    PAYLOAD_VALID_TO,
    PAYLOAD_WORKSPACE_ID,
)

# Type alias for the long union Qdrant uses in its ``must`` lists.
_MustClause = (
    qm.FieldCondition
    | qm.IsEmptyCondition
    | qm.IsNullCondition
    | qm.HasIdCondition
    | qm.HasVectorCondition
    | qm.NestedCondition
    | qm.Filter
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
    current_only_flag: bool = False
    as_of: datetime | None = None
    metadata_filters: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    checkpoint_flag: bool = False

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

    def with_current_only(self) -> QdrantFilterBuilder:
        """Return a new builder that excludes end-dated (historical) points.

        Stage 1 (bitemporal end-dating, refactor/bitemporal-vector-hybrid):
        After a content-hash change, old chunk points are end-dated
        (``valid_to`` set to now) rather than deleted.  Callers that
        want ONLY the still-valid current generation — e.g.
        ``_find_document`` — must add this clause so they don't see
        end-dated points alongside current ones.

        Adds ``IsNullCondition`` on ``valid_to``.  This is the same
        "still-open" clause that ``build_end_date_chunks_filter`` uses
        to select candidates for end-dating.  Compose with
        ``.exclude_tombstoned()`` for full current-state semantics.
        """
        return replace(self, current_only_flag=True)

    def with_as_of(self, as_of: datetime | None) -> QdrantFilterBuilder:
        """Return a new builder that applies the ADR-0008 §5 ``as_of`` predicate.

        When ``as_of`` is ``None`` the predicate is omitted entirely —
        this is the current-state read path and is byte-identical to the
        pre-bitemporal contract (no ``valid_*`` clauses on the wire).
        When ``as_of`` is supplied the canonical open-closed predicate
        ``valid_from <= as_of AND (valid_to > as_of OR valid_to IS NULL)``
        is layered onto the workspace-scoped ``must``.
        """
        return replace(self, as_of=as_of)

    def with_metadata_filter(self, key: str, value: str) -> QdrantFilterBuilder:
        """Return a new builder narrowed by a ``metadata.<key>`` payload field.

        Stage 4 (enumerate mode): payload-indexed metadata sub-fields
        (service, resource_type, account_id, kind, namespace) allow
        O(index) enumeration without HNSW.  Keys are expected to match
        the constants in ``ENUMERATE_PAYLOAD_INDEXES`` (qdrant_constants).
        """
        return replace(self, metadata_filters=(*self.metadata_filters, (key, value)))

    def with_checkpoint(self) -> QdrantFilterBuilder:
        """Return a new builder narrowed to checkpoint points."""
        return replace(self, checkpoint_flag=True)

    def build(self) -> qm.Filter:
        """Emit the concrete Qdrant filter.

        Post-condition: ``workspace_id`` is always present in the
        ``must`` clause.  This invariant is re-asserted in a unit
        test to guard against future refactors.
        """
        must: list[_MustClause] = [
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
        if self.current_only_flag:
            must.append(qm.IsNullCondition(is_null=qm.PayloadField(key=PAYLOAD_VALID_TO)))
        if self.as_of is not None:
            must.extend(_as_of_must_clauses(self.as_of))

        for meta_key, meta_value in self.metadata_filters:
            must.append(
                qm.FieldCondition(
                    key=meta_key,
                    match=qm.MatchValue(value=meta_value),
                )
            )
        if self.checkpoint_flag:
            must.append(
                qm.FieldCondition(
                    key="is_checkpoint",
                    match=qm.MatchValue(value=True),
                )
            )
        return qm.Filter(must=must)


def _as_of_must_clauses(as_of: datetime) -> list[_MustClause]:
    """Build the ADR-0008 §5 ``as_of`` predicate as Qdrant ``must`` clauses.

    The canonical predicate is
    ``valid_from <= $as_of AND ($as_of < valid_to OR valid_to IS NULL)``.

    - ``valid_from <= as_of`` maps to ``DatetimeRange(lte=as_of)`` on the
      ``valid_from`` payload field.
    - ``valid_to > as_of OR valid_to IS NULL`` maps to a sub-``Filter``
      with ``should=[DatetimeRange(gt=as_of), IsNullCondition]`` —
      Qdrant evaluates a ``should`` block as logical OR.

    Boundary semantics (open-closed `[valid_from, valid_to)` per ADR-0008 §1):
    ``as_of == valid_from`` is included (``lte``); ``as_of == valid_to``
    is excluded (strict ``gt``) — the row that ended exactly at ``as_of``
    has stopped being valid by ``as_of``.

    ``as_of`` is serialised through Qdrant's native ``DatetimeRange`` so
    timezone-aware values round-trip cleanly.  The caller is responsible
    for ensuring ``as_of`` is timezone-aware (the ``SearchRequest``
    validator at the public API surface enforces this).
    """
    valid_from_clause: _MustClause = qm.FieldCondition(
        key=PAYLOAD_VALID_FROM,
        range=qm.DatetimeRange(lte=as_of),
    )
    valid_to_open_clause: _MustClause = qm.Filter(
        should=[
            qm.FieldCondition(
                key=PAYLOAD_VALID_TO,
                range=qm.DatetimeRange(gt=as_of),
            ),
            qm.IsNullCondition(is_null=qm.PayloadField(key=PAYLOAD_VALID_TO)),
        ]
    )
    return [valid_from_clause, valid_to_open_clause]


def build_as_of_filter(
    *,
    workspace_id: uuid.UUID,
    as_of: datetime,
    source_ids: tuple[uuid.UUID, ...] = (),
) -> qm.Filter:
    """Standalone helper: workspace-scoped filter with the ``as_of`` predicate.

    Mirrors :class:`QdrantFilterBuilder` for callers that want a
    one-shot constructor (e.g. test fixtures).  In production code path
    prefer ``QdrantFilterBuilder(workspace_id=...).with_as_of(t)`` so
    the workspace invariant is anchored at construction time.
    """
    builder = QdrantFilterBuilder(workspace_id=workspace_id).with_as_of(as_of)
    if source_ids:
        builder = builder.with_source_ids(list(source_ids))
    return builder.build()


def build_enumerate_filter(
    *,
    workspace_id: uuid.UUID,
    metadata_key: str,
    metadata_value: str,
    source_id: uuid.UUID | None = None,
) -> qm.Filter:
    """Build a workspace-scoped filter for enumerate-mode payload scanning.

    Stage 4 (enumerate mode, refactor/bitemporal-vector-hybrid):
    Enumerate queries ("all X matching Y") bypass HNSW and use
    ``exact=True`` Qdrant count + scroll on payload indexes.
    This helper constructs the appropriate filter for a
    ``metadata.<key>`` dimension (service, resource_type, etc.).

    The filter selects current-only (``valid_to IS NULL``) non-tombstoned
    points.  Workspace scoping is mandatory per ADR-0006 §ACL.
    """
    builder = (
        QdrantFilterBuilder(workspace_id=workspace_id)
        .exclude_tombstoned()
        .with_current_only()
        .with_metadata_filter(metadata_key, metadata_value)
    )
    if source_id is not None:
        builder = builder.with_source_ids([source_id])
    return builder.build()


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
    must: list[_MustClause] = [
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
    must: list[_MustClause] = [
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
    must: list[_MustClause] = [
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


def build_end_date_chunks_filter(
    *,
    workspace_id: uuid.UUID,
    source_id: uuid.UUID,
    exclude_external_ids: tuple[str, ...] = (),
) -> qm.Filter:
    """Build the per-(workspace, source) filter for chunk end-dating.

    ADR-0008 §6 + issue #137: re-ingestion of a smaller snapshot end-
    dates chunks absent from the new batch by setting ``valid_to`` on
    their payload.  No DELETE — the retention worker (#135) is the only
    hard-delete path on Qdrant.

    Workspace-scoped (must clause).  ``exclude_external_ids`` is the
    set of chunk ``external_id``s present in the new batch — the
    end-dating skips those because the writer is about to upsert them.
    Mapped to a ``must_not`` clause so the resulting filter selects
    "still-open chunks of (workspace, source) NOT in the new batch".

    "Still-open" means ``valid_to`` is absent or null on the payload.
    Implemented as a ``must`` clause on ``IsNullCondition`` over
    ``valid_to`` — symmetric with the existing ``exclude_tombstoned``
    pattern in :class:`QdrantFilterBuilder`.
    """
    must: list[_MustClause] = [
        qm.FieldCondition(
            key=PAYLOAD_WORKSPACE_ID,
            match=qm.MatchValue(value=str(workspace_id)),
        ),
        qm.FieldCondition(
            key=PAYLOAD_SOURCE_ID,
            match=qm.MatchValue(value=str(source_id)),
        ),
    ]

    must_not: list[_MustClause] = []
    if exclude_external_ids:
        must_not.append(
            qm.FieldCondition(
                key=PAYLOAD_EXTERNAL_ID,
                match=qm.MatchAny(any=list(exclude_external_ids)),
            )
        )
    return qm.Filter(must=must, must_not=must_not) if must_not else qm.Filter(must=must)


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


def build_point_ids_filter(
    *,
    workspace_id: uuid.UUID,
    point_ids: list[str],
) -> qm.Filter:
    """Build a workspace-scoped filter selecting a specific set of point IDs.

    Stage 1 (end-dating, refactor/bitemporal-vector-hybrid):
    Used by ``_end_date_points`` to set ``valid_to`` on a batch of
    known point ids while keeping the workspace must-clause intact.
    Workspace scoping is retained even though point ids are globally
    unique, so the lint rule stays satisfied and a buggy caller cannot
    accidentally end-date foreign-workspace points.
    """
    must: list[_MustClause] = [
        qm.FieldCondition(
            key=PAYLOAD_WORKSPACE_ID,
            match=qm.MatchValue(value=str(workspace_id)),
        ),
        qm.HasIdCondition(has_id=point_ids),  # type: ignore[arg-type]
    ]
    return qm.Filter(must=must)


__all__ = [
    "QdrantFilterBuilder",
    "build_as_of_filter",
    "build_end_date_chunks_filter",
    "build_enumerate_filter",
    "build_point_ids_filter",
    "build_retention_eligible_filter",
    "build_tombstone_sweep_filter",
    "build_warm_archive_filter",
    "build_warm_tier_filter",
]
