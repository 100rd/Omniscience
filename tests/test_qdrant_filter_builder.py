"""Unit tests for the Qdrant filter-builder (issue #106).

These tests encode the ACL invariant from ADR-0006 §ACL carry-forward
at the source level — they do NOT need a Qdrant cluster to run.

Failure of any of these tests must block merge: the class of bug they
protect against (workspace bypass) is a direct analogue of the one
PR #119 fixed on the graph side.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from omniscience_index.stores.qdrant_constants import (
    PAYLOAD_DOCUMENT_ID,
    PAYLOAD_EXTERNAL_ID,
    PAYLOAD_SOURCE_ID,
    PAYLOAD_TOMBSTONED_AT,
    PAYLOAD_VALID_FROM,
    PAYLOAD_VALID_TO,
    PAYLOAD_WORKSPACE_ID,
)
from omniscience_index.stores.qdrant_filters import (
    QdrantFilterBuilder,
    build_as_of_filter,
    build_tombstone_sweep_filter,
)
from qdrant_client import models as qm

_WS_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
_WS_B = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")


def _field_keys(flt: qm.Filter) -> list[str]:
    """Extract the ``key`` strings of every FieldCondition in ``must``."""
    return [c.key for c in (flt.must or []) if isinstance(c, qm.FieldCondition)]


def test_builder_requires_workspace_id_at_construction() -> None:
    """The builder has no default for ``workspace_id`` — can't instantiate without it."""
    with pytest.raises(TypeError):
        QdrantFilterBuilder()  # type: ignore[call-arg]


def test_build_emits_workspace_id_must_clause() -> None:
    """ACL invariant: every build() output has workspace_id in ``must``."""
    flt = QdrantFilterBuilder(workspace_id=_WS_A).build()
    keys = _field_keys(flt)
    assert PAYLOAD_WORKSPACE_ID in keys
    # ``workspace_id`` is first — it is the ACL anchor, not an afterthought.
    assert keys[0] == PAYLOAD_WORKSPACE_ID


def test_build_workspace_value_matches_constructor_input() -> None:
    flt = QdrantFilterBuilder(workspace_id=_WS_A).build()
    must = flt.must or []
    ws_conds = [
        c for c in must if isinstance(c, qm.FieldCondition) and c.key == PAYLOAD_WORKSPACE_ID
    ]
    assert len(ws_conds) == 1
    match = ws_conds[0].match
    assert isinstance(match, qm.MatchValue)
    assert match.value == str(_WS_A)


def test_with_source_ids_narrows_filter() -> None:
    src = uuid.uuid4()
    flt = QdrantFilterBuilder(workspace_id=_WS_A).with_source_ids([src]).build()
    assert PAYLOAD_SOURCE_ID in _field_keys(flt)
    # Workspace still present after narrowing.
    assert PAYLOAD_WORKSPACE_ID in _field_keys(flt)


def test_with_document_ids_narrows_filter() -> None:
    doc = uuid.uuid4()
    flt = QdrantFilterBuilder(workspace_id=_WS_A).with_document_ids([doc]).build()
    assert PAYLOAD_DOCUMENT_ID in _field_keys(flt)
    assert PAYLOAD_WORKSPACE_ID in _field_keys(flt)


def test_with_external_id_narrows_filter() -> None:
    flt = QdrantFilterBuilder(workspace_id=_WS_A).with_external_id("doc-123").build()
    assert PAYLOAD_EXTERNAL_ID in _field_keys(flt)
    # The external_id value is threaded through unchanged.
    ext = [
        c
        for c in (flt.must or [])
        if isinstance(c, qm.FieldCondition) and c.key == PAYLOAD_EXTERNAL_ID
    ]
    assert isinstance(ext[0].match, qm.MatchValue)
    assert ext[0].match.value == "doc-123"


def test_exclude_tombstoned_adds_is_null_clause() -> None:
    flt = QdrantFilterBuilder(workspace_id=_WS_A).exclude_tombstoned().build()
    null_conds = [c for c in (flt.must or []) if isinstance(c, qm.IsNullCondition)]
    assert len(null_conds) == 1
    assert null_conds[0].is_null.key == PAYLOAD_TOMBSTONED_AT


def test_builder_is_immutable_and_returns_new_instances() -> None:
    """Builders are frozen dataclasses — composition never mutates in-place."""
    b0 = QdrantFilterBuilder(workspace_id=_WS_A)
    b1 = b0.with_source_ids([uuid.uuid4()])
    assert b0 is not b1
    assert b0.source_ids == ()
    assert len(b1.source_ids) == 1
    # workspace_id survives every narrowing step.
    assert b1.workspace_id == _WS_A


def test_workspace_can_never_be_widened_by_composition() -> None:
    """No public API widens workspace; it is fixed at construction time.

    Regression guard for ADR-0006 §ACL: the builder must not expose a
    ``with_workspace_id`` or similar mutator.
    """
    b = QdrantFilterBuilder(workspace_id=_WS_A)
    assert not hasattr(b, "with_workspace_id")
    # dataclasses.replace could widen it programmatically — but callers
    # have no reason to, and the lint guard below catches raw Filter
    # construction elsewhere.


def test_different_workspaces_produce_different_filters() -> None:
    a = QdrantFilterBuilder(workspace_id=_WS_A).build()
    b = QdrantFilterBuilder(workspace_id=_WS_B).build()
    a_match = a.must[0].match  # type: ignore[union-attr, index]
    b_match = b.must[0].match  # type: ignore[union-attr, index]
    assert isinstance(a_match, qm.MatchValue)
    assert isinstance(b_match, qm.MatchValue)
    assert a_match.value != b_match.value


# ---------------------------------------------------------------------------
# ADR-0008 §5 ``as_of`` predicate (issue #134)
# ---------------------------------------------------------------------------


_AS_OF = datetime(2026, 4, 12, 19, 25, 0, tzinfo=UTC)


def _ranges_in_must(flt: qm.Filter) -> list[tuple[str, qm.DatetimeRange]]:
    """Extract ``(key, DatetimeRange)`` pairs from top-level ``must`` clauses."""
    out: list[tuple[str, qm.DatetimeRange]] = []
    for c in flt.must or []:
        if (
            isinstance(c, qm.FieldCondition)
            and c.range is not None
            and isinstance(c.range, qm.DatetimeRange)
        ):
            out.append((c.key, c.range))
    return out


def test_with_as_of_none_omits_predicate_entirely() -> None:
    """``as_of=None`` is the current-state hot path — no ``valid_*`` clauses on the wire."""
    flt = QdrantFilterBuilder(workspace_id=_WS_A).with_as_of(None).build()
    keys = _field_keys(flt)
    assert PAYLOAD_VALID_FROM not in keys
    assert PAYLOAD_VALID_TO not in keys
    # Only the workspace clause is on the wire.
    assert keys == [PAYLOAD_WORKSPACE_ID]


def test_with_as_of_adds_valid_from_lte_clause() -> None:
    """``valid_from <= as_of`` lands as a ``DatetimeRange(lte=as_of)`` clause."""
    flt = QdrantFilterBuilder(workspace_id=_WS_A).with_as_of(_AS_OF).build()
    ranges = dict(_ranges_in_must(flt))
    assert PAYLOAD_VALID_FROM in ranges
    assert ranges[PAYLOAD_VALID_FROM].lte == _AS_OF
    assert ranges[PAYLOAD_VALID_FROM].lt is None


def test_with_as_of_adds_valid_to_gt_or_null_subfilter() -> None:
    """``valid_to > as_of OR valid_to IS NULL`` lands as a ``should``-block sub-filter."""
    flt = QdrantFilterBuilder(workspace_id=_WS_A).with_as_of(_AS_OF).build()
    sub_filters = [c for c in (flt.must or []) if isinstance(c, qm.Filter)]
    assert len(sub_filters) == 1
    sub = sub_filters[0]
    assert sub.should is not None
    # Sub-filter has exactly two should clauses: valid_to gt-range + IsNull.
    range_clauses = [
        c
        for c in sub.should
        if isinstance(c, qm.FieldCondition) and isinstance(c.range, qm.DatetimeRange)
    ]
    null_clauses = [c for c in sub.should if isinstance(c, qm.IsNullCondition)]
    assert len(range_clauses) == 1
    assert range_clauses[0].key == PAYLOAD_VALID_TO
    assert range_clauses[0].range.gt == _AS_OF  # type: ignore[union-attr]
    assert len(null_clauses) == 1
    assert null_clauses[0].is_null.key == PAYLOAD_VALID_TO


def test_workspace_clause_survives_as_of() -> None:
    """ACL invariant holds even when ``as_of`` is layered on."""
    flt = QdrantFilterBuilder(workspace_id=_WS_A).with_as_of(_AS_OF).build()
    keys = _field_keys(flt)
    # Workspace is still the first clause (anchor, not afterthought).
    assert keys[0] == PAYLOAD_WORKSPACE_ID
    workspace_clauses = [
        c
        for c in (flt.must or [])
        if isinstance(c, qm.FieldCondition) and c.key == PAYLOAD_WORKSPACE_ID
    ]
    assert len(workspace_clauses) == 1
    assert isinstance(workspace_clauses[0].match, qm.MatchValue)
    assert workspace_clauses[0].match.value == str(_WS_A)


def test_with_as_of_preserves_other_narrowers() -> None:
    """``with_as_of`` composes with ``with_source_ids`` / ``exclude_tombstoned``."""
    src = uuid.uuid4()
    flt = (
        QdrantFilterBuilder(workspace_id=_WS_A)
        .with_source_ids([src])
        .exclude_tombstoned()
        .with_as_of(_AS_OF)
        .build()
    )
    keys = _field_keys(flt)
    assert PAYLOAD_SOURCE_ID in keys
    assert PAYLOAD_VALID_FROM in keys
    null_conds = [c for c in (flt.must or []) if isinstance(c, qm.IsNullCondition)]
    # Tombstone-null clause is present (excludes tombstoned).
    tombstone_nulls = [c for c in null_conds if c.is_null.key == PAYLOAD_TOMBSTONED_AT]
    assert len(tombstone_nulls) == 1


def test_with_as_of_is_immutable_and_chainable() -> None:
    """``with_as_of`` returns a new builder; the original keeps ``as_of=None``."""
    b0 = QdrantFilterBuilder(workspace_id=_WS_A)
    b1 = b0.with_as_of(_AS_OF)
    assert b0.as_of is None
    assert b1.as_of == _AS_OF
    assert b0 is not b1


def test_build_as_of_filter_is_workspace_scoped() -> None:
    """The standalone helper carries the workspace must-clause, ADR-0006 §ACL."""
    flt = build_as_of_filter(workspace_id=_WS_A, as_of=_AS_OF)
    keys = _field_keys(flt)
    assert PAYLOAD_WORKSPACE_ID in keys
    assert PAYLOAD_VALID_FROM in keys


def test_build_as_of_filter_with_source_ids() -> None:
    """The standalone helper accepts a source-id narrower."""
    src = uuid.uuid4()
    flt = build_as_of_filter(workspace_id=_WS_A, as_of=_AS_OF, source_ids=(src,))
    keys = _field_keys(flt)
    assert PAYLOAD_SOURCE_ID in keys


def test_tombstone_sweep_filter_is_explicitly_global() -> None:
    """The sweep filter is the single sanctioned cross-workspace filter.

    It is used ONLY by ``delete_tombstoned`` (admin cron).  The builder
    module is where this exception lives so the lint rule can allow-list
    a single file.
    """
    flt = build_tombstone_sweep_filter(cutoff_iso="2026-01-01T00:00:00+00:00")
    keys = _field_keys(flt)
    assert PAYLOAD_TOMBSTONED_AT in keys
    assert PAYLOAD_WORKSPACE_ID not in keys  # by design — admin-global
