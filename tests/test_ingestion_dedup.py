"""Tests for the server-side dedup gate (issue #164).

The :class:`omniscience_server.ingestion.dedup.DedupGate` is exercised
against a mocked async sessionmaker — same approach the rest of the
repo takes for SQLAlchemy code (see ``tests/test_index_writer.py``).

Coverage matrix (matches the issue + the prompt's required matrix):

* operator first emit, no prior row -> ACCEPT, authority=operator
* agentic first emit, no prior row -> ACCEPT, authority=agentic
* operator emit, agentic row live -> ACCEPT, transition to operator,
  metric ``operator_supersedes_agentic`` increments.
* agentic emit, operator row live, within TTL -> DROP,
  metric ``no_op_agentic_dropped`` increments.
* operator emit, operator row live (idempotent replay) -> ACCEPT, no churn.
* cross-workspace isolation: ws A operator emit MUST NOT touch ws B.
* race condition: two concurrent operator emits collapse to one ACCEPT
  and one idempotent refresh (PK serialisation guarantees).
* feature flag OFF: dedup_disabled, every event ACCEPTed.
* TTL=0 (sticky): operator authority never reassigned to agentic.
* TTL expired: agentic emit reclaims authority,
  metric ``ttl_reassign_to_agentic`` increments.
* non-K8s emitter: bypasses dedup unconditionally.
* env-var parsing: enabled/disabled/sticky/TTL boundary cases.
* metrics labels: workspace_id is NEVER a label dimension.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from omniscience_server.ingestion.dedup import (
    ACTION_AGENTIC_FLAG_DISABLED,
    ACTION_DEDUP_DISABLED,
    ACTION_FIRST_AUTHORITY_ASSIGNED,
    ACTION_NO_OP_AGENTIC_DROPPED,
    ACTION_NO_OP_IDEMPOTENT_REFRESH,
    ACTION_NON_K8S_BYPASS,
    ACTION_OPERATOR_SUPERSEDES_AGENTIC,
    ACTION_TTL_REASSIGN_TO_AGENTIC,
    DEFAULT_TTL_HOURS,
    EMITTER_AGENTIC,
    EMITTER_OPERATOR,
    DedupAction,
    DedupConfig,
    DedupGate,
    config_from_env,
    is_k8s_emitter,
    parse_kind_from_external_id,
)
from omniscience_server.ingestion.events import DocumentChangeEvent
from omniscience_server.ingestion.metrics import (
    INGESTION_DEDUP_ACTION_TOTAL,
    INGESTION_DEDUP_AUTHORITY_TRANSITIONS_TOTAL,
    INGESTION_DEDUP_DROP_TOTAL,
)

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _make_event(
    *,
    source_type: str = EMITTER_OPERATOR,
    external_id: str | None = None,
    cluster_id: uuid.UUID | None = None,
    kind: str = "Pod",
    namespace: str = "default",
    name: str = "demo",
) -> DocumentChangeEvent:
    """Build a DocumentChangeEvent shaped like the operator's output.

    ``external_id`` follows the operator's canonical form
    ``k8s_resource/<cluster_id>/<Kind>/<namespace>/<name>`` so the
    ``parse_kind_from_external_id`` helper produces a real label.
    """
    if external_id is None:
        if cluster_id is None:
            cluster_id = uuid.uuid4()
        external_id = f"k8s_resource/{cluster_id}/{kind}/{namespace}/{name}"
    return DocumentChangeEvent(
        source_id=uuid.uuid4(),
        source_type=source_type,
        external_id=external_id,
        uri=f"kube://test/{namespace}/{kind}/{name}",
        action="created",
    )


def _make_session(
    *,
    select_row: Any = None,
) -> tuple[MagicMock, MagicMock]:
    """Build a (factory, session) mock pair.

    ``select_row`` is the value returned by the FOR UPDATE select; pass
    ``None`` to simulate no row, or a SimpleNamespace-like object with
    ``authority_emitter`` and ``last_emit_at`` attrs to simulate an
    existing row.
    """
    select_result = MagicMock()
    select_result.first = MagicMock(return_value=select_row)

    update_result = MagicMock()
    update_result.rowcount = 1

    session = AsyncMock()
    # First execute() is the SELECT, subsequent ones are the UPSERT/UPDATE.
    session.execute = AsyncMock(side_effect=[select_result, update_result, update_result])
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(return_value=cm)
    return factory, session


def _row(authority: str, last_emit_at: datetime) -> Any:
    """Build a row-like object with the columns the gate selects."""
    row = MagicMock()
    row.authority_emitter = authority
    row.last_emit_at = last_emit_at
    return row


def _counter(metric: Any, **labels: str) -> float:
    """Snapshot a counter at a given label set."""
    return float(metric.labels(**labels)._value.get())


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestPureHelpers:
    def test_is_k8s_emitter_operator(self) -> None:
        assert is_k8s_emitter(EMITTER_OPERATOR) is True

    def test_is_k8s_emitter_agentic(self) -> None:
        assert is_k8s_emitter(EMITTER_AGENTIC) is True

    def test_is_k8s_emitter_other_returns_false(self) -> None:
        assert is_k8s_emitter("git") is False
        assert is_k8s_emitter("slack") is False
        assert is_k8s_emitter("terraform") is False

    def test_parse_kind_from_namespaced_external_id(self) -> None:
        eid = f"k8s_resource/{uuid.uuid4()}/Pod/default/web-1"
        assert parse_kind_from_external_id(eid) == "Pod"

    def test_parse_kind_from_cluster_scoped_external_id(self) -> None:
        # Cluster-scoped form: k8s_resource/<cluster_id>/<Kind>/<name>
        eid = f"k8s_resource/{uuid.uuid4()}/Node/worker-1"
        assert parse_kind_from_external_id(eid) == "Node"

    def test_parse_kind_unknown_for_non_operator_shape(self) -> None:
        assert parse_kind_from_external_id("git/path/to/file.py") == "unknown"
        assert parse_kind_from_external_id("") == "unknown"
        # Adversarial: looks-similar-but-isnt
        assert parse_kind_from_external_id("k8s_resource//Pod/default/x") == "unknown"


# ---------------------------------------------------------------------------
# config_from_env
# ---------------------------------------------------------------------------


class TestConfigFromEnv:
    def test_defaults_when_unset(self) -> None:
        cfg = config_from_env(enabled_env=None, ttl_hours_env=None)
        assert cfg.enabled is True
        assert cfg.ttl_seconds == DEFAULT_TTL_HOURS * 3600.0

    def test_disabled_via_enabled_env_false(self) -> None:
        cfg = config_from_env(enabled_env="false", ttl_hours_env=None)
        assert cfg.enabled is False

    def test_disabled_via_ttl_minus_one(self) -> None:
        cfg = config_from_env(enabled_env=None, ttl_hours_env="-1")
        assert cfg.enabled is False

    def test_sticky_via_ttl_zero(self) -> None:
        cfg = config_from_env(enabled_env=None, ttl_hours_env="0")
        assert cfg.enabled is True
        assert cfg.ttl_seconds is None

    def test_custom_positive_ttl(self) -> None:
        cfg = config_from_env(enabled_env=None, ttl_hours_env="2.5")
        assert cfg.enabled is True
        assert cfg.ttl_seconds == 2.5 * 3600.0

    def test_unparseable_ttl_falls_back_to_default(self) -> None:
        cfg = config_from_env(enabled_env=None, ttl_hours_env="not-a-number")
        assert cfg.enabled is True
        assert cfg.ttl_seconds == DEFAULT_TTL_HOURS * 3600.0

    def test_negative_other_than_minus_one_disables(self) -> None:
        cfg = config_from_env(enabled_env=None, ttl_hours_env="-42")
        assert cfg.enabled is False

    def test_enabled_falsy_synonyms(self) -> None:
        for raw in ["false", "FALSE", "0", "no", "off", ""]:
            cfg = config_from_env(enabled_env=raw, ttl_hours_env=None)
            assert cfg.enabled is False, raw

    # ── #216: OMNISCIENCE_K8S_AGENTIC_ALLOWED parsing ──────────────────────

    def test_agentic_allowed_default_true_when_unset(self) -> None:
        cfg = config_from_env(enabled_env=None, ttl_hours_env=None)
        assert cfg.agentic_allowed is True

    def test_agentic_allowed_explicit_true_passes_through(self) -> None:
        cfg = config_from_env(enabled_env=None, ttl_hours_env=None, agentic_allowed_env="true")
        assert cfg.agentic_allowed is True

    def test_agentic_allowed_falsy_synonyms(self) -> None:
        for raw in ["false", "FALSE", "0", "no", "off", ""]:
            cfg = config_from_env(enabled_env=None, ttl_hours_env=None, agentic_allowed_env=raw)
            assert cfg.agentic_allowed is False, raw

    def test_agentic_allowed_independent_of_dedup_enabled(self) -> None:
        # Disabling dedup must NOT also disable the agentic-allowed flag.
        # The two dials are orthogonal; the flag is enforced regardless of
        # the dedup gate's enabled state.
        cfg = config_from_env(enabled_env="false", ttl_hours_env=None, agentic_allowed_env="false")
        assert cfg.enabled is False
        assert cfg.agentic_allowed is False


# ---------------------------------------------------------------------------
# #216: agentic-allowed flag short-circuit at the gate boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAgenticAllowedFlag:
    """The flag drops agentic events at the gate before any DB lookup,
    leaves operator and non-K8s events alone, and is enforced even when
    the dedup gate itself is disabled."""

    async def test_flag_false_drops_agentic_event_before_db_lookup(self) -> None:
        factory, session = _make_session(select_row=None)
        gate = DedupGate(
            factory,
            DedupConfig(enabled=True, ttl_seconds=86400.0, agentic_allowed=False),
        )
        ws = uuid.uuid4()
        event = _make_event(source_type=EMITTER_AGENTIC)

        before = _counter(
            INGESTION_DEDUP_ACTION_TOTAL,
            kind="Pod",
            action=ACTION_AGENTIC_FLAG_DISABLED,
        )
        decision = await gate.evaluate(workspace_id=ws, event=event)
        after = _counter(
            INGESTION_DEDUP_ACTION_TOTAL,
            kind="Pod",
            action=ACTION_AGENTIC_FLAG_DISABLED,
        )

        assert decision.action == DedupAction.drop
        assert decision.reason == ACTION_AGENTIC_FLAG_DISABLED
        assert decision.authority_emitter == EMITTER_OPERATOR
        assert after == before + 1
        # Critical: NO database query happened.  The session factory was
        # never invoked because the short-circuit returned before the
        # ``async with`` block.
        factory.assert_not_called()
        session.execute.assert_not_called()

    async def test_flag_false_leaves_operator_event_unchanged(self) -> None:
        factory, _ = _make_session(select_row=None)
        gate = DedupGate(
            factory,
            DedupConfig(enabled=True, ttl_seconds=86400.0, agentic_allowed=False),
        )
        event = _make_event(source_type=EMITTER_OPERATOR)

        decision = await gate.evaluate(workspace_id=uuid.uuid4(), event=event)

        assert decision.action == DedupAction.accept
        # Operator event went through the normal first-write path.
        assert decision.reason == ACTION_FIRST_AUTHORITY_ASSIGNED
        factory.assert_called_once()

    async def test_flag_false_leaves_non_k8s_event_unchanged(self) -> None:
        factory, _ = _make_session(select_row=None)
        gate = DedupGate(
            factory,
            DedupConfig(enabled=True, ttl_seconds=86400.0, agentic_allowed=False),
        )
        event = _make_event(source_type="git", external_id="git/path/to/file.py")

        decision = await gate.evaluate(workspace_id=uuid.uuid4(), event=event)

        # Non-K8s emitter takes the existing non-K8s bypass path.
        assert decision.action == DedupAction.accept
        assert decision.reason == ACTION_NON_K8S_BYPASS
        # Non-K8s bypass also short-circuits before the DB lookup.
        factory.assert_not_called()

    async def test_flag_false_enforced_even_when_dedup_disabled(self) -> None:
        # The flag check runs BEFORE the dedup-disabled short-circuit, so
        # an operator that has dedup off in a debug env still gets agentic
        # events dropped when the v0.4 flag is set.
        factory, _session = _make_session(select_row=None)
        gate = DedupGate(
            factory,
            DedupConfig(enabled=False, ttl_seconds=None, agentic_allowed=False),
        )
        event = _make_event(source_type=EMITTER_AGENTIC)

        decision = await gate.evaluate(workspace_id=uuid.uuid4(), event=event)

        assert decision.action == DedupAction.drop
        assert decision.reason == ACTION_AGENTIC_FLAG_DISABLED
        factory.assert_not_called()


# ---------------------------------------------------------------------------
# Empty table: first-write paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestFirstWrites:
    async def test_operator_first_emit_no_prior_row_accepts(self) -> None:
        factory, session = _make_session(select_row=None)
        gate = DedupGate(factory, DedupConfig(enabled=True, ttl_seconds=86400.0))
        ws = uuid.uuid4()
        event = _make_event(source_type=EMITTER_OPERATOR)

        before = _counter(
            INGESTION_DEDUP_ACTION_TOTAL,
            kind="Pod",
            action=ACTION_FIRST_AUTHORITY_ASSIGNED,
        )
        decision = await gate.evaluate(workspace_id=ws, event=event)
        after = _counter(
            INGESTION_DEDUP_ACTION_TOTAL,
            kind="Pod",
            action=ACTION_FIRST_AUTHORITY_ASSIGNED,
        )

        assert decision.action == DedupAction.accept
        assert decision.reason == ACTION_FIRST_AUTHORITY_ASSIGNED
        assert decision.authority_emitter == EMITTER_OPERATOR
        assert decision.kind == "Pod"
        assert after == before + 1
        # Two execute calls: SELECT + INSERT ... ON CONFLICT
        assert session.execute.await_count == 2
        session.commit.assert_awaited_once()

    async def test_agentic_first_emit_no_prior_row_accepts(self) -> None:
        factory, _session = _make_session(select_row=None)
        gate = DedupGate(factory, DedupConfig(enabled=True, ttl_seconds=86400.0))
        event = _make_event(source_type=EMITTER_AGENTIC)

        decision = await gate.evaluate(workspace_id=uuid.uuid4(), event=event)
        assert decision.action == DedupAction.accept
        assert decision.authority_emitter == EMITTER_AGENTIC


# ---------------------------------------------------------------------------
# Operator-supersedes-agentic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestOperatorSupersedesAgentic:
    async def test_operator_emit_with_agentic_authority_transitions(self) -> None:
        existing = _row(EMITTER_AGENTIC, datetime.now(UTC))
        factory, session = _make_session(select_row=existing)
        gate = DedupGate(factory, DedupConfig(enabled=True, ttl_seconds=86400.0))
        event = _make_event(source_type=EMITTER_OPERATOR)

        before_super = _counter(
            INGESTION_DEDUP_ACTION_TOTAL,
            kind="Pod",
            action=ACTION_OPERATOR_SUPERSEDES_AGENTIC,
        )
        before_trans = _counter(
            INGESTION_DEDUP_AUTHORITY_TRANSITIONS_TOTAL,
            kind="Pod",
            from_emitter=EMITTER_AGENTIC,
            to_emitter=EMITTER_OPERATOR,
        )
        decision = await gate.evaluate(workspace_id=uuid.uuid4(), event=event)
        after_super = _counter(
            INGESTION_DEDUP_ACTION_TOTAL,
            kind="Pod",
            action=ACTION_OPERATOR_SUPERSEDES_AGENTIC,
        )
        after_trans = _counter(
            INGESTION_DEDUP_AUTHORITY_TRANSITIONS_TOTAL,
            kind="Pod",
            from_emitter=EMITTER_AGENTIC,
            to_emitter=EMITTER_OPERATOR,
        )

        assert decision.action == DedupAction.accept
        assert decision.reason == ACTION_OPERATOR_SUPERSEDES_AGENTIC
        assert decision.authority_emitter == EMITTER_OPERATOR
        assert after_super == before_super + 1
        assert after_trans == before_trans + 1
        session.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# Agentic dropped under operator authority
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAgenticDropped:
    async def test_agentic_emit_with_operator_authority_within_ttl_drops(self) -> None:
        # last_emit_at = now - 1h; TTL = 24h -> well within window
        recent = datetime.now(UTC) - timedelta(hours=1)
        existing = _row(EMITTER_OPERATOR, recent)
        factory, session = _make_session(select_row=existing)
        gate = DedupGate(factory, DedupConfig(enabled=True, ttl_seconds=86400.0))
        event = _make_event(source_type=EMITTER_AGENTIC)

        before_drop = _counter(
            INGESTION_DEDUP_ACTION_TOTAL,
            kind="Pod",
            action=ACTION_NO_OP_AGENTIC_DROPPED,
        )
        before_drop_metric = _counter(
            INGESTION_DEDUP_DROP_TOTAL,
            kind="Pod",
            dropped_emitter=EMITTER_AGENTIC,
            authority_emitter=EMITTER_OPERATOR,
        )
        decision = await gate.evaluate(workspace_id=uuid.uuid4(), event=event)
        after_drop = _counter(
            INGESTION_DEDUP_ACTION_TOTAL,
            kind="Pod",
            action=ACTION_NO_OP_AGENTIC_DROPPED,
        )
        after_drop_metric = _counter(
            INGESTION_DEDUP_DROP_TOTAL,
            kind="Pod",
            dropped_emitter=EMITTER_AGENTIC,
            authority_emitter=EMITTER_OPERATOR,
        )

        assert decision.action == DedupAction.drop
        assert decision.reason == ACTION_NO_OP_AGENTIC_DROPPED
        assert decision.authority_emitter == EMITTER_OPERATOR
        assert after_drop == before_drop + 1
        assert after_drop_metric == before_drop_metric + 1
        # SELECT only — no UPDATE issued on a drop
        assert session.execute.await_count == 1
        session.rollback.assert_awaited_once()
        session.commit.assert_not_awaited()

    async def test_agentic_emit_under_sticky_authority_always_drops(self) -> None:
        """ttl_seconds=None means sticky operator authority — never reassigned."""
        ancient = datetime.now(UTC) - timedelta(days=365)
        existing = _row(EMITTER_OPERATOR, ancient)
        factory, _ = _make_session(select_row=existing)
        gate = DedupGate(factory, DedupConfig(enabled=True, ttl_seconds=None))
        event = _make_event(source_type=EMITTER_AGENTIC)

        decision = await gate.evaluate(workspace_id=uuid.uuid4(), event=event)
        assert decision.action == DedupAction.drop
        assert decision.authority_emitter == EMITTER_OPERATOR


# ---------------------------------------------------------------------------
# Idempotent operator replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestIdempotentReplay:
    async def test_operator_replay_under_operator_authority_no_op(self) -> None:
        existing = _row(EMITTER_OPERATOR, datetime.now(UTC))
        factory, session = _make_session(select_row=existing)
        gate = DedupGate(factory, DedupConfig(enabled=True, ttl_seconds=86400.0))
        event = _make_event(source_type=EMITTER_OPERATOR)

        before = _counter(
            INGESTION_DEDUP_ACTION_TOTAL,
            kind="Pod",
            action=ACTION_NO_OP_IDEMPOTENT_REFRESH,
        )
        decision = await gate.evaluate(workspace_id=uuid.uuid4(), event=event)
        after = _counter(
            INGESTION_DEDUP_ACTION_TOTAL,
            kind="Pod",
            action=ACTION_NO_OP_IDEMPOTENT_REFRESH,
        )

        assert decision.action == DedupAction.accept
        assert decision.reason == ACTION_NO_OP_IDEMPOTENT_REFRESH
        assert decision.authority_emitter == EMITTER_OPERATOR
        assert after == before + 1
        # SELECT + UPDATE last_emit_at — exactly two execute calls.
        assert session.execute.await_count == 2

    async def test_agentic_replay_under_agentic_authority_no_op(self) -> None:
        existing = _row(EMITTER_AGENTIC, datetime.now(UTC))
        factory, _session = _make_session(select_row=existing)
        gate = DedupGate(factory, DedupConfig(enabled=True, ttl_seconds=86400.0))
        event = _make_event(source_type=EMITTER_AGENTIC)

        decision = await gate.evaluate(workspace_id=uuid.uuid4(), event=event)
        assert decision.action == DedupAction.accept
        assert decision.reason == ACTION_NO_OP_IDEMPOTENT_REFRESH
        assert decision.authority_emitter == EMITTER_AGENTIC


# ---------------------------------------------------------------------------
# TTL-driven re-assignment (operator -> agentic)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestTtlReassignment:
    async def test_agentic_after_operator_ttl_expired_reclaims(self) -> None:
        # last_emit_at older than TTL -> agentic should reclaim.
        ttl_seconds = 60.0
        wall_now = datetime.now(UTC)
        ancient = wall_now - timedelta(seconds=ttl_seconds * 2)
        existing = _row(EMITTER_OPERATOR, ancient)
        factory, session = _make_session(select_row=existing)
        gate = DedupGate(factory, DedupConfig(enabled=True, ttl_seconds=ttl_seconds))
        event = _make_event(source_type=EMITTER_AGENTIC)

        before = _counter(
            INGESTION_DEDUP_ACTION_TOTAL,
            kind="Pod",
            action=ACTION_TTL_REASSIGN_TO_AGENTIC,
        )
        decision = await gate.evaluate(workspace_id=uuid.uuid4(), event=event, now=wall_now)
        after = _counter(
            INGESTION_DEDUP_ACTION_TOTAL,
            kind="Pod",
            action=ACTION_TTL_REASSIGN_TO_AGENTIC,
        )

        assert decision.action == DedupAction.accept
        assert decision.reason == ACTION_TTL_REASSIGN_TO_AGENTIC
        assert decision.authority_emitter == EMITTER_AGENTIC
        assert after == before + 1
        session.commit.assert_awaited_once()

    async def test_agentic_at_ttl_boundary_still_drops(self) -> None:
        """At exactly TTL, the operator counts as 'still alive' — the
        comparator is strict (<), not (<=). This matches the issue's
        '> now - 24h' wording ('STRICT greater than')."""
        ttl_seconds = 60.0
        wall_now = datetime.now(UTC)
        cutoff = wall_now - timedelta(seconds=ttl_seconds)
        existing = _row(EMITTER_OPERATOR, cutoff)  # exactly at boundary
        factory, _ = _make_session(select_row=existing)
        gate = DedupGate(factory, DedupConfig(enabled=True, ttl_seconds=ttl_seconds))
        event = _make_event(source_type=EMITTER_AGENTIC)

        decision = await gate.evaluate(workspace_id=uuid.uuid4(), event=event, now=wall_now)
        assert decision.action == DedupAction.drop


# ---------------------------------------------------------------------------
# Cross-workspace isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCrossWorkspaceIsolation:
    async def test_workspaces_use_independent_lookups(self) -> None:
        """Two evaluations against different workspace_ids must each
        consult the row for their own workspace.

        We verify the SELECT bind parameter for ``workspace_id`` matches
        the caller's argument exactly — no cross-workspace lookup is
        possible by construction (PK is composite).
        """
        # ws_a has agentic row; ws_b is empty.  Both events emit the
        # SAME external_id; correct behaviour is that each workspace's
        # decision is driven only by its own row.
        ws_a = uuid.uuid4()
        ws_b = uuid.uuid4()
        external_id = "k8s_resource/00000000-0000-0000-0000-000000000099/Pod/default/x"

        # ---- ws_a: agentic row exists, operator emit -> supersedes
        existing = _row(EMITTER_AGENTIC, datetime.now(UTC))
        factory_a, session_a = _make_session(select_row=existing)
        gate_a = DedupGate(factory_a, DedupConfig(enabled=True, ttl_seconds=86400.0))
        event_a = _make_event(source_type=EMITTER_OPERATOR, external_id=external_id)
        decision_a = await gate_a.evaluate(workspace_id=ws_a, event=event_a)
        assert decision_a.action == DedupAction.accept
        assert decision_a.reason == ACTION_OPERATOR_SUPERSEDES_AGENTIC

        # The SELECT call MUST have been bound to ws_a — never ws_b.
        select_call = session_a.execute.await_args_list[0]
        bind_params = select_call.args[1]
        assert bind_params["workspace_id"] == ws_a
        assert bind_params["external_id"] == external_id

        # ---- ws_b: no row, agentic emit -> first authority assignment
        factory_b, session_b = _make_session(select_row=None)
        gate_b = DedupGate(factory_b, DedupConfig(enabled=True, ttl_seconds=86400.0))
        event_b = _make_event(source_type=EMITTER_AGENTIC, external_id=external_id)
        decision_b = await gate_b.evaluate(workspace_id=ws_b, event=event_b)
        assert decision_b.action == DedupAction.accept
        assert decision_b.reason == ACTION_FIRST_AUTHORITY_ASSIGNED
        assert decision_b.authority_emitter == EMITTER_AGENTIC

        select_call_b = session_b.execute.await_args_list[0]
        assert select_call_b.args[1]["workspace_id"] == ws_b


# ---------------------------------------------------------------------------
# Concurrency: PK serialisation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestConcurrentEvents:
    async def test_concurrent_operator_emits_serialise_via_pk(self) -> None:
        """Two operator events for the same key arrive simultaneously.

        With ``SELECT ... FOR UPDATE`` the second transaction blocks
        until the first commits.  In our mocked session both calls run
        sequentially anyway; the contract being tested is that the
        SELECT statement uses ``FOR UPDATE`` (text presence) so the
        production engine serialises naturally.
        """
        # First call: no row -> first-authority insertion.
        factory_1, session_1 = _make_session(select_row=None)
        gate_1 = DedupGate(factory_1, DedupConfig(enabled=True, ttl_seconds=86400.0))
        ws = uuid.uuid4()
        event = _make_event(source_type=EMITTER_OPERATOR)
        await gate_1.evaluate(workspace_id=ws, event=event)

        # The SELECT statement string must contain ``FOR UPDATE`` so the
        # production Postgres engine serialises concurrent writers.
        first_call = session_1.execute.await_args_list[0]
        select_sql = str(first_call.args[0])
        assert "FOR UPDATE" in select_sql.upper()


# ---------------------------------------------------------------------------
# Feature flag: dedup disabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestFeatureFlagOff:
    async def test_disabled_skips_db_lookup_and_accepts(self) -> None:
        # Even with a phantom existing row, disabled means accept.
        factory = MagicMock()
        gate = DedupGate(factory, DedupConfig(enabled=False, ttl_seconds=86400.0))
        event = _make_event(source_type=EMITTER_AGENTIC)

        before = _counter(
            INGESTION_DEDUP_ACTION_TOTAL,
            kind="Pod",
            action=ACTION_DEDUP_DISABLED,
        )
        decision = await gate.evaluate(workspace_id=uuid.uuid4(), event=event)
        after = _counter(
            INGESTION_DEDUP_ACTION_TOTAL,
            kind="Pod",
            action=ACTION_DEDUP_DISABLED,
        )

        assert decision.action == DedupAction.accept
        assert decision.reason == ACTION_DEDUP_DISABLED
        assert after == before + 1
        # No session opened
        factory.assert_not_called()


# ---------------------------------------------------------------------------
# Non-K8s emitter passthrough
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestNonK8sBypass:
    async def test_git_event_bypasses_dedup(self) -> None:
        factory = MagicMock()
        gate = DedupGate(factory, DedupConfig(enabled=True, ttl_seconds=86400.0))
        event = DocumentChangeEvent(
            source_id=uuid.uuid4(),
            source_type="git",
            external_id="path/to/file.py",
            uri="file://path/to/file.py",
            action="created",
        )

        before = _counter(
            INGESTION_DEDUP_ACTION_TOTAL,
            kind="unknown",
            action=ACTION_NON_K8S_BYPASS,
        )
        decision = await gate.evaluate(workspace_id=uuid.uuid4(), event=event)
        after = _counter(
            INGESTION_DEDUP_ACTION_TOTAL,
            kind="unknown",
            action=ACTION_NON_K8S_BYPASS,
        )

        assert decision.action == DedupAction.accept
        assert decision.reason == ACTION_NON_K8S_BYPASS
        assert after == before + 1
        factory.assert_not_called()


# ---------------------------------------------------------------------------
# Metrics labelling: workspace_id NEVER on labels (security)
# ---------------------------------------------------------------------------


class TestMetricLabels:
    def test_drop_counter_has_no_workspace_label(self) -> None:
        labels = list(INGESTION_DEDUP_DROP_TOTAL._labelnames)
        assert "workspace_id" not in labels
        assert "workspace" not in labels
        assert "tenant_id" not in labels
        assert set(labels) == {"kind", "dropped_emitter", "authority_emitter"}

    def test_action_counter_has_no_workspace_label(self) -> None:
        labels = list(INGESTION_DEDUP_ACTION_TOTAL._labelnames)
        assert "workspace_id" not in labels
        assert set(labels) == {"kind", "action"}

    def test_transitions_counter_has_no_workspace_label(self) -> None:
        labels = list(INGESTION_DEDUP_AUTHORITY_TRANSITIONS_TOTAL._labelnames)
        assert "workspace_id" not in labels
        assert set(labels) == {"kind", "from_emitter", "to_emitter"}


# ---------------------------------------------------------------------------
# Worker integration — gate is called and DROP short-circuits the pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestWorkerIntegration:
    """End-to-end: dedup decision is honoured by IngestionWorker.process_document.

    Reuses the test scaffolding pattern from ``tests/test_ingestion.py`` —
    every external is mocked.  The interesting assertions are: when the
    gate returns DROP, the pipeline never runs; when it returns ACCEPT,
    the pipeline runs as before.
    """

    @staticmethod
    def _make_worker(
        gate: Any,
        *,
        upsert_action: str = "created",
    ) -> Any:
        """Build an IngestionWorker with all collaborators mocked."""
        from omniscience_core.db.models import Source
        from omniscience_server.ingestion.worker import IngestionWorker

        # Source row used by _resolve_workspace.
        src = MagicMock(spec=Source)
        src.id = uuid.uuid4()
        src.tenant_id = uuid.uuid4()
        src.name = "test-source"

        # Session factory used by _resolve_workspace's _fetch_source.
        scalar = MagicMock()
        scalar.scalar_one_or_none = MagicMock(return_value=src)
        session = AsyncMock()
        session.execute = AsyncMock(return_value=scalar)
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=False)
        sess_factory = MagicMock(return_value=cm)

        # Connector
        from omniscience_connectors.base import DocumentRef, FetchedDocument

        connector = MagicMock()
        connector.fetch = AsyncMock(
            return_value=FetchedDocument(
                ref=DocumentRef(external_id="x", uri="kube://x"),
                content_bytes=b"hello",
                content_type="application/json",
            )
        )
        registry = MagicMock()
        registry.get = MagicMock(return_value=connector)

        # Embedding provider
        embed = MagicMock()
        embed.dim = 4
        embed.model_name = "test-model"
        embed.provider_name = "test-provider"
        embed.embed = AsyncMock(return_value=[[0.1, 0.2, 0.3, 0.4]])

        # Index writer
        writer_result = MagicMock()
        writer_result.action = upsert_action
        writer_result.chunks_written = 1
        writer_result.document_id = uuid.uuid4()
        writer = MagicMock()
        writer.upsert_document = AsyncMock(return_value=writer_result)
        writer.tombstone = AsyncMock(return_value=True)

        # Stores
        graph = MagicMock()
        graph.upsert_graph = AsyncMock()
        graph.delete_tombstoned = AsyncMock()
        vector = MagicMock()
        vector.upsert_chunks = AsyncMock(
            return_value=MagicMock(action="created", chunks_written=1, document_id=uuid.uuid4())
        )
        vector.delete_by_document = AsyncMock()

        # Queue consumer (only used by .start(); we call process_document directly)
        consumer = MagicMock()

        worker = IngestionWorker(
            queue_consumer=consumer,
            connector_registry=registry,
            embedding_provider=embed,
            index_writer=writer,
            graph_store=graph,
            vector_store=vector,
            session_factory=sess_factory,
            secrets_resolver=None,
            dedup_gate=gate,
        )
        return worker, connector, registry

    async def test_drop_decision_short_circuits_pipeline(self) -> None:
        from omniscience_server.ingestion.dedup import DedupAction, DedupDecision

        # Gate that always drops.
        gate = MagicMock()
        gate.evaluate = AsyncMock(
            return_value=DedupDecision(
                action=DedupAction.drop,
                reason=ACTION_NO_OP_AGENTIC_DROPPED,
                authority_emitter=EMITTER_OPERATOR,
                kind="Pod",
            )
        )
        worker, connector, registry = self._make_worker(gate)
        event = _make_event(source_type=EMITTER_AGENTIC)
        result = await worker.process_document(event)
        assert result.action == "dedup_dropped"
        # Pipeline was never invoked
        registry.get.assert_not_called()
        connector.fetch.assert_not_awaited()

    async def test_accept_decision_runs_pipeline(self) -> None:
        from omniscience_server.ingestion.dedup import DedupAction, DedupDecision

        gate = MagicMock()
        gate.evaluate = AsyncMock(
            return_value=DedupDecision(
                action=DedupAction.accept,
                reason=ACTION_FIRST_AUTHORITY_ASSIGNED,
                authority_emitter=EMITTER_OPERATOR,
                kind="Pod",
            )
        )
        worker, connector, registry = self._make_worker(gate, upsert_action="created")
        event = _make_event(source_type=EMITTER_OPERATOR)
        result = await worker.process_document(event)
        # The pipeline succeeded -> action is the upsert action
        assert result.action == "created"
        registry.get.assert_called_once()
        connector.fetch.assert_awaited_once()

    async def test_delete_action_bypasses_dedup_gate(self) -> None:
        """Deletes are not dedup'd; tombstoning is idempotent."""
        gate = MagicMock()
        gate.evaluate = AsyncMock()
        worker, _, _registry = self._make_worker(gate)
        event = DocumentChangeEvent(
            source_id=uuid.uuid4(),
            source_type=EMITTER_AGENTIC,
            external_id="k8s_resource/abc/Pod/default/x",
            uri="kube://x",
            action="deleted",
        )
        await worker.process_document(event)
        # The gate must NOT have been consulted on a delete
        gate.evaluate.assert_not_awaited()


# ---------------------------------------------------------------------------
# Env-var construction (worker default path)
# ---------------------------------------------------------------------------


class TestWorkerEnvDefaults:
    def test_default_env_produces_enabled_gate_with_24h_ttl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OMNISCIENCE_DEDUP_ENABLED", raising=False)
        monkeypatch.delenv("OMNISCIENCE_INGEST_DEDUP_TTL_HOURS", raising=False)

        cfg = config_from_env(
            enabled_env=os.environ.get("OMNISCIENCE_DEDUP_ENABLED"),
            ttl_hours_env=os.environ.get("OMNISCIENCE_INGEST_DEDUP_TTL_HOURS"),
        )
        assert cfg.enabled is True
        assert cfg.ttl_seconds == DEFAULT_TTL_HOURS * 3600.0

    def test_disabled_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OMNISCIENCE_DEDUP_ENABLED", "false")
        cfg = config_from_env(
            enabled_env=os.environ.get("OMNISCIENCE_DEDUP_ENABLED"),
            ttl_hours_env=None,
        )
        assert cfg.enabled is False
