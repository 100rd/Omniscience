"""End-to-end integration test for the replay primitive (issue #243).

Acceptance gate (issue #243):

* Stage 3 entity-state mutations between t0 and t2.
* Replay at t1 returns t1-state; replay at t2 returns t2-state.
* The two replays carry different ``state_fingerprint`` hashes.
* Two replays at the same anchor produce identical fingerprints.

Why testcontainers
------------------

The replay handler delegates to the existing
``GraphStore.get_entity`` / ``find_related`` paths, which carry the
ADR-0008 §5 bitemporal predicate in Cypher.  The only way to assert
that determinism end-to-end (across the Neo4j 5.x semantics, the
Cypher predicate, the response serialisation, the canonical-JSON
encoding and the BLAKE2b digest) is to exercise it against a real
Neo4j 5.x.

Skip pattern matches ``tests/integration/test_neo4j_bootstrap_schema.py``:
unconditional run when Docker + testcontainers-neo4j are present, hard
skip otherwise.  We do NOT gate behind
``OMNISCIENCE_RUN_NEO4J_CONTRACT_TESTS=1`` because the issue #243
contract has to hold on every PR.

Bitemporal staging strategy
---------------------------

Rather than going through the writer (which couples this test to
ingestion-layer concerns + the metadata serialisation path already
covered by ``test_neo4j_writer_metadata.py``), we plant the
``:Entity`` + ``:EntityState`` chain directly via Cypher.  Three
states are created with overlapping ``[valid_from, valid_to)``
intervals so each anchor falls in a distinct window:

* t0       — initial state ``kind = "service:v1"``.
* t1 = t0+1h — mutation 1, ``kind = "service:v2"``.
* t2 = t0+2h — mutation 2, ``kind = "service:v3"`` (current).

Replay at ``t0 + 30m`` resolves to v1.
Replay at ``t1 + 30m`` resolves to v2.
Replay at ``t2 + 30m`` resolves to v3.

The FastAPI app fixture is the lightest one possible — just a
``FastAPI()`` with the Neo4j-backed ``GraphStore`` wired to
``app.state.graph_store``.  No full app, no DB, no MCP server.
That keeps the test focused on the replay invariant.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI

pytest.importorskip("testcontainers.neo4j")

from omniscience_core.audit.fingerprint import (
    FINGERPRINT_HEX_LENGTH,
    compute_state_fingerprint,
)
from omniscience_index.stores.neo4j_store import (
    Neo4jGraphStore,
    Neo4jStoreConfig,
    _run_write_stmt,
)
from omniscience_server.replay import (
    SUPPORTED_REPLAY_TOOLS,
    ReplayQuery,
    replay_context,
)

pytestmark = pytest.mark.asyncio


_NEO4J_PASSWORD = "issue_243_password"
_WORKSPACE_ID = uuid.UUID("24300000-0000-0000-0000-000000000243")
_ENTITY_NAME = "svc.payments.replay-fixture"

# Three bitemporal anchors.  Microsecond-stable so re-running the test
# never crosses an interval boundary due to clock-skew.
_T0 = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
_T1 = _T0 + timedelta(hours=1)
_T2 = _T0 + timedelta(hours=2)

# Sampling anchors — chosen INSIDE each [valid_from, valid_to) window
# so the bitemporal predicate is unambiguous.  ADR-0008 §5 — interval
# is open-closed; valid_to is exclusive.
_PROBE_AT_T0_WINDOW = _T0 + timedelta(minutes=30)
_PROBE_AT_T1_WINDOW = _T1 + timedelta(minutes=30)
_PROBE_AT_T2_WINDOW = _T2 + timedelta(minutes=30)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def neo4j_store() -> AsyncIterator[Neo4jGraphStore]:
    """Spin up a fresh Neo4j 5.19-community container and connect to it."""
    from testcontainers.neo4j import Neo4jContainer  # type: ignore[import-not-found]

    container = Neo4jContainer("neo4j:5.19-community", password=_NEO4J_PASSWORD)
    with container:
        config = Neo4jStoreConfig(
            uri=container.get_connection_url(),
            username="neo4j",
            password=_NEO4J_PASSWORD,
            database="neo4j",
            max_connection_pool_size=5,
            connection_acquisition_timeout_seconds=30.0,
            max_transaction_retry_time_seconds=15.0,
            default_max_depth=3,
        )
        store = Neo4jGraphStore(config=config)
        await store.connect()
        try:
            yield store
        finally:
            await store.close()


@pytest.fixture
async def staged_app(neo4j_store: Neo4jGraphStore) -> AsyncIterator[FastAPI]:
    """A minimal FastAPI app with the Neo4j store wired in, pre-staged.

    The three :EntityState rows are planted before the app is yielded
    so every test reaches a deterministic graph state.  No teardown is
    needed — the Neo4j container is destroyed by ``neo4j_store``.
    """
    await _plant_three_entity_states(neo4j_store)
    app = FastAPI()
    app.state.graph_store = neo4j_store
    yield app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _plant_three_entity_states(store: Neo4jGraphStore) -> None:
    """Plant ``:Entity`` + 3 ``:EntityState`` rows on the live container.

    Open-closed intervals (ADR-0008 §5):

    * v1: [t0, t1)
    * v2: [t1, t2)
    * v3: [t2, NULL)   — still current

    The ``:Entity`` identity mirror carries the v3 attributes per
    ADR-0008 §2 (it's the latest-version cache).
    """
    workspace_id = str(_WORKSPACE_ID)
    source_id = "src-replay-243"
    entity_id = str(uuid.uuid4())

    # 1) The identity mirror.
    cypher_entity = (
        "MERGE (n:Entity {workspace_id: $workspace_id, id: $entity_id, name: $name}) "
        "SET n.kind = $kind_v3, n.source_id = $source_id, "
        "    n.chunk_text = $chunk_text, "
        "    n.valid_from = datetime($t2), n.valid_to = NULL, "
        "    n.recorded_at = datetime($t2)"
    )
    params_entity = {
        "workspace_id": workspace_id,
        "entity_id": entity_id,
        "name": _ENTITY_NAME,
        "kind_v3": "service:v3",
        "source_id": source_id,
        "chunk_text": "Payments service replay fixture",
        "t2": _T2.isoformat(),
    }
    async with store._driver.session(database=store._config.database) as session:
        await session.execute_write(_run_write_stmt, cypher_entity, params_entity)

    # 2) Three :EntityState rows, each linked via :HAD_STATE.
    state_template = (
        "MATCH (n:Entity {workspace_id: $workspace_id, id: $entity_id}) "
        "MERGE (n)-[:HAD_STATE]->(s:EntityState {workspace_id: $workspace_id, "
        "    id: $state_id}) "
        "SET s.kind = $kind, s.source_id = $source_id, "
        "    s.valid_from = datetime($valid_from), "
        "    s.valid_to = $valid_to_expr, "
        "    s.recorded_at = datetime($recorded_at)"
    )
    versions: list[dict[str, Any]] = [
        {
            "state_id": "state-v1",
            "kind": "service:v1",
            "valid_from": _T0.isoformat(),
            "valid_to_expr": _T1,
            "recorded_at": _T0.isoformat(),
        },
        {
            "state_id": "state-v2",
            "kind": "service:v2",
            "valid_from": _T1.isoformat(),
            "valid_to_expr": _T2,
            "recorded_at": _T1.isoformat(),
        },
        {
            "state_id": "state-v3",
            "kind": "service:v3",
            "valid_from": _T2.isoformat(),
            "valid_to_expr": None,  # still current
            "recorded_at": _T2.isoformat(),
        },
    ]

    async with store._driver.session(database=store._config.database) as session:
        for ver in versions:
            # valid_to is either a Cypher datetime() or NULL; the driver
            # cannot parametrise the function call itself, so we pre-bake
            # the parameter dict with a Python None / datetime object and
            # rely on the driver's native conversion.
            valid_to_param = ver["valid_to_expr"]
            cypher = state_template.replace(
                "$valid_to_expr",
                ("datetime($valid_to)" if valid_to_param is not None else "NULL"),
            )
            params: dict[str, Any] = {
                "workspace_id": workspace_id,
                "entity_id": entity_id,
                "state_id": ver["state_id"],
                "kind": ver["kind"],
                "source_id": source_id,
                "valid_from": ver["valid_from"],
                "recorded_at": ver["recorded_at"],
            }
            if valid_to_param is not None:
                params["valid_to"] = (
                    valid_to_param.isoformat()
                    if isinstance(valid_to_param, datetime)
                    else valid_to_param
                )
            await session.execute_write(_run_write_stmt, cypher, params)


async def _replay_get_entity(
    *,
    app: FastAPI,
    at_time: datetime,
) -> tuple[dict[str, Any], str]:
    """Run a ``get_entity`` replay and return (response, fingerprint)."""
    result = await replay_context(
        app=app,
        workspace_id=_WORKSPACE_ID,
        at_time=at_time,
        query=ReplayQuery(
            tool_name="get_entity",
            arguments={"entity_name": _ENTITY_NAME},
        ),
    )
    return result.response, result.state_fingerprint


# ---------------------------------------------------------------------------
# Unit-level pure tests (no container) — invariants the integration test
# leans on.  Cheap to run; gate against later regressions in the hashing
# module that would silently break the determinism contract.
# ---------------------------------------------------------------------------


async def test_state_fingerprint_is_deterministic_on_dict_order() -> None:
    """Reordering dict keys must not change the fingerprint.

    The replay envelope's determinism guarantee rests on canonical JSON;
    if a future serialiser swap drops ``sort_keys`` this assertion
    breaks the build.
    """
    a = {"x": 1, "y": [1, 2, 3], "z": {"inner": True}}
    b = {"z": {"inner": True}, "y": [1, 2, 3], "x": 1}
    fa = compute_state_fingerprint(a)
    fb = compute_state_fingerprint(b)
    assert fa == fb
    assert len(fa) == FINGERPRINT_HEX_LENGTH


async def test_state_fingerprint_changes_on_value_change() -> None:
    """Different payloads MUST yield different fingerprints (collision-free)."""
    fa = compute_state_fingerprint({"k": 1})
    fb = compute_state_fingerprint({"k": 2})
    assert fa != fb


async def test_supported_replay_tools_includes_v0_5_surface() -> None:
    """The v0.5 replay surface MUST cover the four production tools."""
    expected = {"search", "get_entity", "get_related_entities", "resolve_incident"}
    assert expected.issubset(set(SUPPORTED_REPLAY_TOOLS))


# ---------------------------------------------------------------------------
# Integration tests — exercise the full Cypher+envelope+hash stack
# ---------------------------------------------------------------------------


async def test_replay_returns_distinct_states_at_t1_and_t2(
    staged_app: FastAPI,
) -> None:
    """The headline #243 acceptance test.

    Replay at the t1 window MUST resolve to v2; replay at the t2 window
    MUST resolve to v3.  Their fingerprints MUST differ.
    """
    response_t1, fp_t1 = await _replay_get_entity(app=staged_app, at_time=_PROBE_AT_T1_WINDOW)
    response_t2, fp_t2 = await _replay_get_entity(app=staged_app, at_time=_PROBE_AT_T2_WINDOW)

    # Sanity check: the bitemporal predicate gave us the right :EntityState.
    assert response_t1["entity"] is not None, "v2 row should exist at t1+30m"
    assert response_t2["entity"] is not None, "v3 row should exist at t2+30m"
    assert response_t1["entity"]["kind"] == "service:v2", (
        f"Expected kind=service:v2 at t1+30m, got {response_t1['entity']['kind']!r}"
    )
    assert response_t2["entity"]["kind"] == "service:v3", (
        f"Expected kind=service:v3 at t2+30m, got {response_t2['entity']['kind']!r}"
    )

    # The whole point of the primitive: distinct state -> distinct hash.
    assert fp_t1 != fp_t2, (
        "state_fingerprint collided across distinct bitemporal anchors — "
        "the replay determinism contract is broken"
    )


async def test_replay_returns_v1_state_at_t0_window(staged_app: FastAPI) -> None:
    """The third mutation in the acceptance criteria — anchor inside v1.

    Confirms the predicate handles the BEGINNING of the timeline, not
    just the latest two windows.  Together with the t1/t2 test, this
    covers all three staged mutations.
    """
    response_t0, fp_t0 = await _replay_get_entity(app=staged_app, at_time=_PROBE_AT_T0_WINDOW)
    assert response_t0["entity"] is not None
    assert response_t0["entity"]["kind"] == "service:v1"

    # Cross-check against t1 to make sure all three windows are unique.
    _, fp_t1 = await _replay_get_entity(app=staged_app, at_time=_PROBE_AT_T1_WINDOW)
    _, fp_t2 = await _replay_get_entity(app=staged_app, at_time=_PROBE_AT_T2_WINDOW)
    fingerprints = {fp_t0, fp_t1, fp_t2}
    assert len(fingerprints) == 3, (
        f"Expected three distinct fingerprints across t0/t1/t2, got {fingerprints}"
    )


async def test_replay_is_deterministic_at_same_anchor(staged_app: FastAPI) -> None:
    """The deterministic-hash contract — same anchor, same hash, always.

    Two replays in the same test, same anchor: identical fingerprints
    or the v0.5 ADP contract is broken (#243 acceptance bullet 3).
    """
    response_a, fp_a = await _replay_get_entity(app=staged_app, at_time=_PROBE_AT_T1_WINDOW)
    response_b, fp_b = await _replay_get_entity(app=staged_app, at_time=_PROBE_AT_T1_WINDOW)

    assert fp_a == fp_b, "replay fingerprint drifted at the same bitemporal anchor"
    # Also assert the responses are equal — the fingerprint binds to
    # the response, so equality must round-trip.
    assert response_a == response_b


async def test_replay_envelope_carries_metadata_for_admin_ui(
    staged_app: FastAPI,
) -> None:
    """The envelope must expose tool_name / at_time / algorithm fields.

    The admin UI's time-machine view (#243 acceptance bullet 6) reads
    these fields; this test pins the contract so a refactor of
    :func:`envelope_to_dict` does not silently break the page.
    """
    result = await replay_context(
        app=staged_app,
        workspace_id=_WORKSPACE_ID,
        at_time=_PROBE_AT_T2_WINDOW,
        query=ReplayQuery(
            tool_name="get_entity",
            arguments={"entity_name": _ENTITY_NAME},
        ),
    )
    assert result.tool_name == "get_entity"
    assert result.at_time == _PROBE_AT_T2_WINDOW
    assert len(result.state_fingerprint) == FINGERPRINT_HEX_LENGTH
    assert result.fingerprint_algorithm.startswith("blake2b-256")
    # No audit row supplied -> match field is None, original is None.
    assert result.original_state_fingerprint is None
    assert result.fingerprint_match is None
    assert result.audit_log_id is None
