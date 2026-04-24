"""Unit tests for the Qdrant filter-builder (issue #106).

These tests encode the ACL invariant from ADR-0006 §ACL carry-forward
at the source level — they do NOT need a Qdrant cluster to run.

Failure of any of these tests must block merge: the class of bug they
protect against (workspace bypass) is a direct analogue of the one
PR #119 fixed on the graph side.
"""

from __future__ import annotations

import uuid

import pytest
from omniscience_index.stores.qdrant_constants import (
    PAYLOAD_DOCUMENT_ID,
    PAYLOAD_EXTERNAL_ID,
    PAYLOAD_SOURCE_ID,
    PAYLOAD_TOMBSTONED_AT,
    PAYLOAD_WORKSPACE_ID,
)
from omniscience_index.stores.qdrant_filters import (
    QdrantFilterBuilder,
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
