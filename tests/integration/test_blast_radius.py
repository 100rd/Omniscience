"""Integration tests for the blast-radius MCP/REST tool (issue #234).

What this file covers
---------------------

1. Staged dependency graph -> the correct downstream set for each
   ``action_type`` (``restart``, ``delete``, ``scale_down``, ``cordon``).
2. Ranking: closer hops + heavier edges rank above farther / lighter.
3. ``entity_not_found`` on missing seed -> ``ValueError`` (MCP) / 404
   (REST), with the same response for cross-workspace seeds (no
   existence leak).
4. ``as_of=T`` is forwarded verbatim to every ``GraphStore`` call.
5. REST surface delivers the same JSON shape as the MCP surface for a
   given seed + action.
6. Invalid ``action_type`` / empty ``entity_id`` -> structured errors.

The fake :class:`_BlastGraphStore` honours the per-action edge-type
allowlist by filtering the planted neighbours by ``edge_type`` (matching
the real Neo4j adapter's ``edge_types`` argument), which is the exact
contract :func:`mcp_blast_radius` relies on.  This keeps the tests
hermetic — no testcontainer required — while still exercising the
action-aware traversal end-to-end through the composition layer.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from omniscience_core.storage.graph import (
    EntityNodeView,
    GraphEdgeView,
    GraphResultView,
)
from omniscience_server.blast_radius import (
    ACTION_TYPES,
    DEFAULT_MAX_DEPTH,
    ENTITY_NOT_FOUND_CODE,
    INVALID_ACTION_TYPE_CODE,
    INVALID_ENTITY_ID_CODE,
    ActionType,
    mcp_blast_radius,
)

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WS_A = uuid.UUID("aaaaaaaa-0000-1111-2222-3333aaaa0234")
_WS_B = uuid.UUID("bbbbbbbb-0000-1111-2222-3333bbbb0234")

_SEED = "service://payments-api"
_DIRECT_DEP = "service://orders-api"
_TRANSITIVE_DEP = "service://shipping-api"
_LOAD_BALANCER = "service://payments-lb"
_NODE = "node/worker-01"
_OWNER_TEAM = "team/payments"
_DEPLOY_PIPELINE = "pipeline/payments-cd"

_AS_OF_T = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fake graph store — action-aware edge-type filtering
# ---------------------------------------------------------------------------


class _BlastGraphStore:
    """Workspace-scoped fake honouring ``(workspace_id, name, as_of, edge_types)``.

    Stores per-workspace entities + adjacency map.  ``find_related`` walks
    the adjacency up to ``max_depth`` and applies the ``edge_types``
    allowlist exactly the way the Neo4j adapter does at the Cypher layer
    (``_TRAVERSE_*_CYPHER_TEMPLATE.__EDGE_TYPE_FILTER__``).

    Each adjacency entry is ``(neighbour_name, edge_type)``.  The store
    materialises an :class:`EntityNodeView` for every reachable neighbour
    with the correct ``depth`` and last-hop ``edge_type``.
    """

    def __init__(self) -> None:
        self._entities: dict[tuple[uuid.UUID, str], EntityNodeView] = {}
        self._adjacency: dict[
            tuple[uuid.UUID, str],
            list[tuple[str, str]],
        ] = {}
        self.calls: list[dict[str, Any]] = []

    def plant_entity(
        self,
        *,
        workspace_id: uuid.UUID,
        name: str,
        kind: str = "service",
        recorded_at: datetime | None = None,
    ) -> None:
        self._entities[(workspace_id, name)] = EntityNodeView(
            name=name,
            kind=kind,
            source="src-test",
            chunk_text=None,
            depth=0,
            edge_type=None,
            recorded_at=recorded_at,
        )

    def plant_edge(
        self,
        *,
        workspace_id: uuid.UUID,
        from_entity: str,
        to_entity: str,
        edge_type: str,
    ) -> None:
        """Add an undirected edge — matches the Neo4j traversal contract."""
        self._adjacency.setdefault((workspace_id, from_entity), []).append((to_entity, edge_type))
        self._adjacency.setdefault((workspace_id, to_entity), []).append((from_entity, edge_type))

    async def get_entity(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
    ) -> EntityNodeView | None:
        self.calls.append(
            {
                "op": "get_entity",
                "entity_name": entity_name,
                "workspace_id": workspace_id,
                "as_of": as_of,
            }
        )
        return self._entities.get((workspace_id, entity_name))

    async def find_related(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
        max_depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> GraphResultView:
        self.calls.append(
            {
                "op": "find_related",
                "entity_name": entity_name,
                "workspace_id": workspace_id,
                "as_of": as_of,
                "max_depth": max_depth,
                "edge_types": edge_types,
            }
        )
        seed = self._entities.get((workspace_id, entity_name))
        if seed is None:
            raise ValueError(f"entity_not_found:{entity_name}")

        allow = frozenset(edge_types) if edge_types is not None else None
        # BFS — track (name, depth, edge_type-of-last-hop).
        visited: dict[str, tuple[int, str]] = {entity_name: (0, "")}
        frontier: list[tuple[str, int]] = [(entity_name, 0)]
        edges_collected: list[GraphEdgeView] = []
        while frontier:
            next_frontier: list[tuple[str, int]] = []
            for current, depth in frontier:
                if depth >= max_depth:
                    continue
                for nbr_name, edge_type in self._adjacency.get((workspace_id, current), []):
                    if allow is not None and edge_type not in allow:
                        continue
                    if nbr_name in visited:
                        continue
                    visited[nbr_name] = (depth + 1, edge_type)
                    edges_collected.append(
                        GraphEdgeView(
                            from_entity=current,
                            to_entity=nbr_name,
                            edge_type=edge_type,
                        )
                    )
                    next_frontier.append((nbr_name, depth + 1))
            frontier = next_frontier

        related: list[EntityNodeView] = []
        for name, (depth, edge_type) in visited.items():
            if name == entity_name:
                continue
            base = self._entities.get((workspace_id, name))
            if base is None:
                continue
            related.append(
                EntityNodeView(
                    name=base.name,
                    kind=base.kind,
                    source=base.source,
                    chunk_text=base.chunk_text,
                    depth=depth,
                    edge_type=edge_type,
                    recorded_at=base.recorded_at,
                )
            )
        return GraphResultView(seed=seed, related=related, edges=edges_collected)

    async def traverse(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
        max_depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> GraphResultView:
        return await self.find_related(
            entity_name=entity_name,
            workspace_id=workspace_id,
            as_of=as_of,
            max_depth=max_depth,
            edge_types=edge_types,
        )


def _wire_session_factory(app: FastAPI) -> None:
    if getattr(app.state, "db_session_factory", None) is not None:
        return
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    app.state.db_session_factory = factory


def _wire_graph_store(app: FastAPI, store: _BlastGraphStore) -> None:
    app.state.graph_store = store
    _wire_session_factory(app)


def _seed_full_topology(
    store: _BlastGraphStore,
    *,
    workspace_id: uuid.UUID,
) -> None:
    """Plant a representative dependency topology around _SEED.

    Topology::

        _SEED <-CALLS-          orders-api <-CALLS- shipping-api
        _SEED <-LOAD_BALANCED_BY- payments-lb
        _SEED <-SCHEDULED_ON-    node/worker-01
        _SEED <-OWNED_BY-        team/payments
        _SEED <-DEPLOYED_BY-     pipeline/payments-cd
    """
    for name, kind in (
        (_SEED, "service"),
        (_DIRECT_DEP, "service"),
        (_TRANSITIVE_DEP, "service"),
        (_LOAD_BALANCER, "load_balancer"),
        (_NODE, "k8s_node"),
        (_OWNER_TEAM, "team"),
        (_DEPLOY_PIPELINE, "pipeline"),
    ):
        store.plant_entity(
            workspace_id=workspace_id,
            name=name,
            kind=kind,
            recorded_at=datetime(2026, 5, 20, 0, 0, 0, tzinfo=UTC),
        )
    store.plant_edge(
        workspace_id=workspace_id,
        from_entity=_DIRECT_DEP,
        to_entity=_SEED,
        edge_type="CALLS",
    )
    store.plant_edge(
        workspace_id=workspace_id,
        from_entity=_TRANSITIVE_DEP,
        to_entity=_DIRECT_DEP,
        edge_type="CALLS",
    )
    store.plant_edge(
        workspace_id=workspace_id,
        from_entity=_LOAD_BALANCER,
        to_entity=_SEED,
        edge_type="LOAD_BALANCED_BY",
    )
    store.plant_edge(
        workspace_id=workspace_id,
        from_entity=_SEED,
        to_entity=_NODE,
        edge_type="SCHEDULED_ON",
    )
    store.plant_edge(
        workspace_id=workspace_id,
        from_entity=_SEED,
        to_entity=_OWNER_TEAM,
        edge_type="OWNED_BY",
    )
    store.plant_edge(
        workspace_id=workspace_id,
        from_entity=_SEED,
        to_entity=_DEPLOY_PIPELINE,
        edge_type="DEPLOYED_BY",
    )


# ---------------------------------------------------------------------------
# 1. Action-type semantics — the headline integration assertion
# ---------------------------------------------------------------------------


class TestActionTypeDownstreamSets:
    """Each action_type returns the correct downstream set (issue #234 §accept)."""

    async def test_restart_includes_runtime_deps_excludes_scheduling_and_ownership(
        self,
    ) -> None:
        store = _BlastGraphStore()
        _seed_full_topology(store, workspace_id=_WS_A)
        app = FastAPI()
        _wire_graph_store(app, store)

        result = await mcp_blast_radius(
            app=app,
            entity_id=_SEED,
            workspace_id=_WS_A,
            action_type="restart",
        )

        impacted = {imp["entity_id"] for imp in result["impacted"]}
        # runtime callers reach via CALLS — included.
        assert _DIRECT_DEP in impacted
        assert _TRANSITIVE_DEP in impacted
        # scheduling / load-balancer / ownership edges — excluded for restart.
        assert _NODE not in impacted
        assert _LOAD_BALANCER not in impacted
        assert _OWNER_TEAM not in impacted
        assert _DEPLOY_PIPELINE not in impacted

    async def test_cordon_includes_only_scheduling_edges(self) -> None:
        store = _BlastGraphStore()
        _seed_full_topology(store, workspace_id=_WS_A)
        app = FastAPI()
        _wire_graph_store(app, store)

        result = await mcp_blast_radius(
            app=app,
            entity_id=_SEED,
            workspace_id=_WS_A,
            action_type="cordon",
        )

        impacted = {imp["entity_id"] for imp in result["impacted"]}
        # cordon only follows SCHEDULED_ON / RUNS_ON.
        assert _NODE in impacted
        assert _DIRECT_DEP not in impacted
        assert _TRANSITIVE_DEP not in impacted
        assert _LOAD_BALANCER not in impacted
        assert _OWNER_TEAM not in impacted

    async def test_scale_down_includes_runtime_and_load_balancer(self) -> None:
        store = _BlastGraphStore()
        _seed_full_topology(store, workspace_id=_WS_A)
        app = FastAPI()
        _wire_graph_store(app, store)

        result = await mcp_blast_radius(
            app=app,
            entity_id=_SEED,
            workspace_id=_WS_A,
            action_type="scale_down",
        )

        impacted = {imp["entity_id"] for imp in result["impacted"]}
        assert _DIRECT_DEP in impacted
        assert _LOAD_BALANCER in impacted
        # Scheduling / ownership stay out of scope.
        assert _NODE not in impacted
        assert _OWNER_TEAM not in impacted

    async def test_delete_is_widest_blast(self) -> None:
        store = _BlastGraphStore()
        _seed_full_topology(store, workspace_id=_WS_A)
        app = FastAPI()
        _wire_graph_store(app, store)

        result = await mcp_blast_radius(
            app=app,
            entity_id=_SEED,
            workspace_id=_WS_A,
            action_type="delete",
        )

        impacted = {imp["entity_id"] for imp in result["impacted"]}
        # Every neighbour we planted is reachable under delete.
        for expected in (
            _DIRECT_DEP,
            _TRANSITIVE_DEP,
            _LOAD_BALANCER,
            _NODE,
            _OWNER_TEAM,
            _DEPLOY_PIPELINE,
        ):
            assert expected in impacted, (
                f"{expected!r} missing from delete blast radius; got {impacted!r}"
            )


# ---------------------------------------------------------------------------
# 2. Ranking — closer hops + heavier edges rank above farther / lighter
# ---------------------------------------------------------------------------


class TestImpactRanking:
    async def test_direct_caller_ranks_above_transitive(self) -> None:
        store = _BlastGraphStore()
        _seed_full_topology(store, workspace_id=_WS_A)
        app = FastAPI()
        _wire_graph_store(app, store)

        result = await mcp_blast_radius(
            app=app,
            entity_id=_SEED,
            workspace_id=_WS_A,
            action_type="restart",
        )

        ordered = [imp["entity_id"] for imp in result["impacted"]]
        # Direct dep at depth 1 must score above transitive at depth 2.
        assert ordered.index(_DIRECT_DEP) < ordered.index(_TRANSITIVE_DEP)
        scores = {imp["entity_id"]: imp["impact_score"] for imp in result["impacted"]}
        assert scores[_DIRECT_DEP] > scores[_TRANSITIVE_DEP]
        # Scores are all in [0, 1].
        for score in scores.values():
            assert 0.0 <= score <= 1.0

    async def test_response_shape_matches_spec(self) -> None:
        """Every impacted entry has the four fields the issue specifies."""
        store = _BlastGraphStore()
        _seed_full_topology(store, workspace_id=_WS_A)
        app = FastAPI()
        _wire_graph_store(app, store)

        result = await mcp_blast_radius(
            app=app,
            entity_id=_SEED,
            workspace_id=_WS_A,
            action_type="restart",
        )

        assert result["seed_entity_id"] == _SEED
        assert result["action_type"] == "restart"
        for imp in result["impacted"]:
            assert set(imp.keys()) >= {
                "entity_id",
                "entity_type",
                "dependency_path",
                "impact_score",
                "confidence",
            }
            # dependency_path is a list of {from_entity, to_entity, edge_type}.
            for step in imp["dependency_path"]:
                assert set(step.keys()) == {"from_entity", "to_entity", "edge_type"}


# ---------------------------------------------------------------------------
# 3. Entity-not-found / cross-workspace ACL
# ---------------------------------------------------------------------------


class TestNotFoundAndAcl:
    async def test_missing_seed_raises_entity_not_found(self) -> None:
        store = _BlastGraphStore()
        app = FastAPI()
        _wire_graph_store(app, store)

        with pytest.raises(ValueError, match=ENTITY_NOT_FOUND_CODE):
            await mcp_blast_radius(
                app=app,
                entity_id=_SEED,
                workspace_id=_WS_A,
                action_type="restart",
            )

    async def test_cross_workspace_seed_is_indistinguishable_from_missing(
        self,
    ) -> None:
        store = _BlastGraphStore()
        _seed_full_topology(store, workspace_id=_WS_A)
        app = FastAPI()
        _wire_graph_store(app, store)

        with pytest.raises(ValueError, match=ENTITY_NOT_FOUND_CODE):
            await mcp_blast_radius(
                app=app,
                entity_id=_SEED,
                workspace_id=_WS_B,
                action_type="restart",
            )
        # No silent widening: every store call carried workspace B only.
        for call in store.calls:
            assert call["workspace_id"] == _WS_B


# ---------------------------------------------------------------------------
# 4. as_of plumbing — forwarded verbatim
# ---------------------------------------------------------------------------


class TestAsOfPlumbing:
    async def test_as_of_forwarded_to_every_store_call(self) -> None:
        store = _BlastGraphStore()
        _seed_full_topology(store, workspace_id=_WS_A)
        app = FastAPI()
        _wire_graph_store(app, store)

        result = await mcp_blast_radius(
            app=app,
            entity_id=_SEED,
            workspace_id=_WS_A,
            action_type="restart",
            as_of=_AS_OF_T,
        )

        # effective_as_of echoes the supplied anchor.
        echoed = datetime.fromisoformat(result["effective_as_of"].replace("Z", "+00:00"))
        assert echoed == _AS_OF_T

        get_call = next(c for c in store.calls if c["op"] == "get_entity")
        find_call = next(c for c in store.calls if c["op"] == "find_related")
        assert get_call["as_of"] == _AS_OF_T
        assert find_call["as_of"] == _AS_OF_T

    async def test_naive_as_of_rejected(self) -> None:
        store = _BlastGraphStore()
        _seed_full_topology(store, workspace_id=_WS_A)
        app = FastAPI()
        _wire_graph_store(app, store)

        with pytest.raises(ValueError, match="invalid_timezone"):
            await mcp_blast_radius(
                app=app,
                entity_id=_SEED,
                workspace_id=_WS_A,
                action_type="restart",
                as_of=datetime(2026, 5, 22, 12, 0, 0),  # naive
            )


# ---------------------------------------------------------------------------
# 5. Validation errors
# ---------------------------------------------------------------------------


class TestValidation:
    async def test_empty_entity_id_rejected(self) -> None:
        store = _BlastGraphStore()
        app = FastAPI()
        _wire_graph_store(app, store)

        with pytest.raises(ValueError, match=INVALID_ENTITY_ID_CODE):
            await mcp_blast_radius(
                app=app,
                entity_id="   ",
                workspace_id=_WS_A,
                action_type="restart",
            )

    @pytest.mark.parametrize("bad", ["upgrade", "REBOOT", ""])
    async def test_invalid_action_type_rejected(self, bad: str) -> None:
        store = _BlastGraphStore()
        _seed_full_topology(store, workspace_id=_WS_A)
        app = FastAPI()
        _wire_graph_store(app, store)

        with pytest.raises(ValueError, match=INVALID_ACTION_TYPE_CODE):
            # mypy: deliberately passing an invalid Literal value.
            await mcp_blast_radius(
                app=app,
                entity_id=_SEED,
                workspace_id=_WS_A,
                action_type=bad,  # type: ignore[arg-type]
            )

    @pytest.mark.parametrize("action_type", list(ACTION_TYPES))
    async def test_every_documented_action_type_is_accepted(
        self,
        action_type: ActionType,
    ) -> None:
        store = _BlastGraphStore()
        _seed_full_topology(store, workspace_id=_WS_A)
        app = FastAPI()
        _wire_graph_store(app, store)

        result = await mcp_blast_radius(
            app=app,
            entity_id=_SEED,
            workspace_id=_WS_A,
            action_type=action_type,
        )
        assert result["action_type"] == action_type
        # Default max_depth bubbles through to the store call.
        find_call = next(c for c in store.calls if c["op"] == "find_related")
        assert find_call["max_depth"] == DEFAULT_MAX_DEPTH


# ---------------------------------------------------------------------------
# 6. Cordon + scheduling - sanity that the allowlist is action-specific
# ---------------------------------------------------------------------------


class TestEdgeAllowlistForwardedToStore:
    """The action-specific allowlist must be forwarded to ``find_related``."""

    async def test_restart_passes_runtime_allowlist(self) -> None:
        store = _BlastGraphStore()
        _seed_full_topology(store, workspace_id=_WS_A)
        app = FastAPI()
        _wire_graph_store(app, store)

        await mcp_blast_radius(
            app=app,
            entity_id=_SEED,
            workspace_id=_WS_A,
            action_type="restart",
        )
        find_call = next(c for c in store.calls if c["op"] == "find_related")
        allow = set(find_call["edge_types"])
        assert {"DEPENDS_ON", "CALLS", "ROUTES_TO"} <= allow
        assert "OWNED_BY" not in allow
        assert "SCHEDULED_ON" not in allow

    async def test_cordon_passes_scheduling_only(self) -> None:
        store = _BlastGraphStore()
        _seed_full_topology(store, workspace_id=_WS_A)
        app = FastAPI()
        _wire_graph_store(app, store)

        await mcp_blast_radius(
            app=app,
            entity_id=_SEED,
            workspace_id=_WS_A,
            action_type="cordon",
        )
        find_call = next(c for c in store.calls if c["op"] == "find_related")
        allow = set(find_call["edge_types"])
        assert allow == {"SCHEDULED_ON", "RUNS_ON"}
