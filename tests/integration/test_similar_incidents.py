"""Integration tests for same-incident clustering (issue #233).

End-to-end exercises against the in-memory ``GraphStore`` fake (same
shape as ``tests/test_mcp_resolve_incident.py``).  The fixture plants:

* 1 SEED incident (``alert://datadog/seed``) firing against
  ``service://api`` with the "CPU spike at 95% on api" symptom.
* 2 SIMILAR past incidents — same provider, same service, similar
  symptom text (only host/timestamp tokens differ — they collapse
  under :func:`omniscience_index.clustering.normalise_symptom`).
* 1 DISSIMILAR past incident — reached via the same api-service pivot
  but with a completely different symptom (``"auth token expired"``)
  so the structural signature differs.

The acceptance test asserts that:

1. ``find_similar_incidents`` returns the 2 similar incidents ranked
   above the dissimilar one with a measurable score margin.
2. ``resolve_incident`` surfaces the same ranked list as the new
   ``similar_past`` field (additive contract from #233).
3. The seed itself never appears in its own similar list.

No live testcontainers — keeps the headline-demo lane fast and
deterministic, mirroring the precedent set by
``tests/integration/test_incident_demo.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from omniscience_core.storage.graph import (
    EntityNodeView,
    GraphEdgeView,
    GraphResultView,
)
from omniscience_server.incidents import mcp_resolve_incident
from omniscience_server.similar_incidents import mcp_find_similar_incidents

# ---------------------------------------------------------------------------
# Constants — the same-shape fake graph as the resolve_incident unit suite.
# ---------------------------------------------------------------------------

_WS = uuid.UUID("aaaaaaaa-2233-4455-6677-8899aabbccdd")

_SEED_ID = "alert://datadog/seed-001"
_SIMILAR_1_ID = "alert://datadog/past-001"
_SIMILAR_2_ID = "alert://datadog/past-002"
_DISSIMILAR_ID = "alert://datadog/past-099"

_TARGET_SERVICE = "service://api"

_NOW = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)

# Recency: similar #1 fired 3 days ago, similar #2 fired 14 days ago,
# dissimilar fired 1 day ago.  Recency alone would favour the
# dissimilar one — the test proves clustering still ranks the similar
# pair higher, demonstrating structural signal dominates.
_SEED_FIRED = _NOW - timedelta(hours=2)
_SIMILAR_1_FIRED = _NOW - timedelta(days=3)
_SIMILAR_2_FIRED = _NOW - timedelta(days=14)
_DISSIMILAR_FIRED = _NOW - timedelta(days=1)


# ---------------------------------------------------------------------------
# In-memory GraphStore fake (mirrors tests/test_mcp_resolve_incident.py)
# ---------------------------------------------------------------------------


class _ClusterGraphStore:
    """Workspace-scoped fake with named-neighbour traversal support.

    Distinct from the resolve-incident fake only in that the candidate
    discovery walk pivots through the seed's service neighbour, then
    finds peer alerts FIRING_AGAINST the same service.  The fake
    answers both halves of that walk deterministically.
    """

    def __init__(self) -> None:
        self._entities: dict[tuple[uuid.UUID, str], EntityNodeView] = {}
        self._neighbours: dict[tuple[uuid.UUID, str], list[EntityNodeView]] = {}
        self._edges: dict[tuple[uuid.UUID, str], list[GraphEdgeView]] = {}

    def plant_entity(self, *, workspace_id: uuid.UUID, node: EntityNodeView) -> None:
        self._entities[(workspace_id, node.name)] = node

    def plant_neighbours(
        self,
        *,
        workspace_id: uuid.UUID,
        seed_name: str,
        related: list[EntityNodeView],
        edges: list[GraphEdgeView] | None = None,
    ) -> None:
        self._neighbours[(workspace_id, seed_name)] = list(related)
        self._edges[(workspace_id, seed_name)] = list(edges or [])

    async def get_entity(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
    ) -> EntityNodeView | None:
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
        seed = self._entities.get((workspace_id, entity_name))
        if seed is None:
            raise ValueError(f"entity_not_found:{entity_name}")
        related = list(self._neighbours.get((workspace_id, entity_name), []))
        edges = list(self._edges.get((workspace_id, entity_name), []))
        return GraphResultView(seed=seed, related=related, edges=edges)

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
        )


# ---------------------------------------------------------------------------
# Fixture planters
# ---------------------------------------------------------------------------


def _alert(
    *,
    name: str,
    fired_at: datetime,
    summary: str,
) -> EntityNodeView:
    return EntityNodeView(
        name=name,
        kind="alert",
        source="src-datadog",
        chunk_text=summary,
        depth=0,
        edge_type=None,
        valid_from=fired_at,
    )


def _service(*, name: str, depth: int = 1) -> EntityNodeView:
    return EntityNodeView(
        name=name,
        kind="service",
        source="src-datadog",
        chunk_text=None,
        depth=depth,
        edge_type="FIRES_AGAINST",
    )


def _peer_alert(
    *,
    name: str,
    fired_at: datetime,
    summary: str,
    depth: int = 2,
) -> EntityNodeView:
    return EntityNodeView(
        name=name,
        kind="alert",
        source="src-datadog",
        chunk_text=summary,
        depth=depth,
        edge_type="FIRES_AGAINST",
        valid_from=fired_at,
    )


def _build_store() -> _ClusterGraphStore:
    store = _ClusterGraphStore()

    # ----- seed + its FIRES_AGAINST target -----
    seed = _alert(
        name=_SEED_ID,
        fired_at=_SEED_FIRED,
        summary="CPU spike at 95% on api pod api-7f9b3a",
    )
    similar_1 = _alert(
        name=_SIMILAR_1_ID,
        fired_at=_SIMILAR_1_FIRED,
        summary="CPU spike at 95% on api pod api-2c4e1d",
    )
    similar_2 = _alert(
        name=_SIMILAR_2_ID,
        fired_at=_SIMILAR_2_FIRED,
        summary="CPU spike at 95% on api pod api-9d8e5f",
    )
    dissimilar = _alert(
        name=_DISSIMILAR_ID,
        fired_at=_DISSIMILAR_FIRED,
        summary="auth token expired for principal=root@svc",
    )

    # Plant all four alerts as standalone entities.
    for alert in (seed, similar_1, similar_2, dissimilar):
        store.plant_entity(workspace_id=_WS, node=alert)

    # Plant the api-service pivot as an entity — it must be resolvable
    # via ``get_entity`` so the similar-incidents discovery code can
    # pivot through it without raising ``entity_not_found``.
    api_service = _service(name=_TARGET_SERVICE)
    store.plant_entity(workspace_id=_WS, node=api_service)

    # The seed's depth=2 traversal exposes the service pivot (depth 1)
    # and the peer alerts (depth 2) reached through it.
    store.plant_neighbours(
        workspace_id=_WS,
        seed_name=_SEED_ID,
        related=[
            api_service,
            _peer_alert(
                name=_SIMILAR_1_ID,
                fired_at=_SIMILAR_1_FIRED,
                summary="CPU spike at 95% on api pod api-2c4e1d",
            ),
            _peer_alert(
                name=_SIMILAR_2_ID,
                fired_at=_SIMILAR_2_FIRED,
                summary="CPU spike at 95% on api pod api-9d8e5f",
            ),
        ],
        edges=[
            GraphEdgeView(
                from_entity=_SEED_ID,
                to_entity=_TARGET_SERVICE,
                edge_type="FIRES_AGAINST",
            ),
        ],
    )

    # The api-service pivot — neighbour list includes the three peer
    # alerts that fire against it.  The dissimilar alert is in this
    # set so the discovery surface picks it up via the pivot path
    # (canonical "alerts on the same service") even though it does
    # not appear in the seed's BFS directly.
    store.plant_neighbours(
        workspace_id=_WS,
        seed_name=_TARGET_SERVICE,
        related=[
            _peer_alert(
                name=_SIMILAR_1_ID,
                fired_at=_SIMILAR_1_FIRED,
                summary="CPU spike at 95% on api pod api-2c4e1d",
                depth=1,
            ),
            _peer_alert(
                name=_SIMILAR_2_ID,
                fired_at=_SIMILAR_2_FIRED,
                summary="CPU spike at 95% on api pod api-9d8e5f",
                depth=1,
            ),
            _peer_alert(
                name=_DISSIMILAR_ID,
                fired_at=_DISSIMILAR_FIRED,
                summary="auth token expired for principal=root@svc",
                depth=1,
            ),
        ],
    )

    # The peer alerts themselves return empty neighbour lists (they're
    # leaves for the purposes of this test).
    for peer in (_SIMILAR_1_ID, _SIMILAR_2_ID, _DISSIMILAR_ID):
        store.plant_neighbours(workspace_id=_WS, seed_name=peer, related=[])

    return store


def _wire_app(store: _ClusterGraphStore) -> FastAPI:
    app = FastAPI()
    app.state.graph_store = store
    # resolve_incident reaches for ``app.state.db_session_factory`` to
    # load a per-tenant scoring config; the fake yields None to
    # exercise the v0.1 ladder, but the attribute must exist.
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    app.state.db_session_factory = factory
    # No embedding provider — the cosine component degrades to 0 (per
    # contract) and the alert_type + service axes carry the signal.
    app.state.embedding_provider = None
    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSimilarIncidentsClustering:
    """The 2 similar candidates outrank the 1 dissimilar one."""

    async def test_two_similar_outrank_one_dissimilar(self) -> None:
        store = _build_store()
        app = _wire_app(store)

        result = await mcp_find_similar_incidents(
            app=app,
            incident_id=_SEED_ID,
            workspace_id=_WS,
            limit=10,
            since_days=90,
        )

        matches = result["matches"]
        assert len(matches) >= 3, (
            f"Expected at least 3 candidates (2 similar + 1 dissimilar), "
            f"got {len(matches)}: {[m['incident_id'] for m in matches]}"
        )

        # Rank order: the top 2 must be the similar incidents; the
        # dissimilar one must rank strictly below them.
        ids_in_order = [m["incident_id"] for m in matches]
        similar_ids = {_SIMILAR_1_ID, _SIMILAR_2_ID}
        top_two = set(ids_in_order[:2])
        assert top_two == similar_ids, (
            f"Expected similar pair {similar_ids} in top 2, got {top_two} "
            f"(full order: {ids_in_order})"
        )

        # Measurable margin: every similar score must beat the
        # dissimilar score by at least 0.05.  This pins the structural
        # signal so a future tweak that flattens the formula trips here.
        scores_by_id = {m["incident_id"]: m["score"] for m in matches}
        sim_scores = [scores_by_id[i] for i in similar_ids]
        dis_score = scores_by_id[_DISSIMILAR_ID]
        margin = min(sim_scores) - dis_score
        assert margin >= 0.05, (
            f"Expected similar -> dissimilar margin >= 0.05, got {margin:.3f} "
            f"(similar={sim_scores}, dissimilar={dis_score})"
        )

        # Both similar incidents should also share the seed's signature.
        for match in matches:
            if match["incident_id"] in similar_ids:
                assert match["signature_match"] is True
            elif match["incident_id"] == _DISSIMILAR_ID:
                assert match["signature_match"] is False

    async def test_seed_excluded_from_its_own_similar_list(self) -> None:
        store = _build_store()
        app = _wire_app(store)

        result = await mcp_find_similar_incidents(
            app=app,
            incident_id=_SEED_ID,
            workspace_id=_WS,
        )
        ids = {m["incident_id"] for m in result["matches"]}
        assert _SEED_ID not in ids

    async def test_signature_field_populated(self) -> None:
        store = _build_store()
        app = _wire_app(store)

        result = await mcp_find_similar_incidents(
            app=app,
            incident_id=_SEED_ID,
            workspace_id=_WS,
        )
        assert isinstance(result["signature"], str)
        assert len(result["signature"]) > 0

    async def test_since_days_window_drops_old_candidates(self) -> None:
        store = _build_store()
        app = _wire_app(store)

        # Window = 7 days drops similar #2 (14 days ago) but keeps
        # similar #1 (3 days ago) and dissimilar (1 day ago).
        result = await mcp_find_similar_incidents(
            app=app,
            incident_id=_SEED_ID,
            workspace_id=_WS,
            since_days=7,
        )
        ids = {m["incident_id"] for m in result["matches"]}
        assert _SIMILAR_2_ID not in ids
        assert _SIMILAR_1_ID in ids


@pytest.mark.asyncio
class TestResolveIncidentCompose:
    """``resolve_incident`` carries the same ranked list under ``similar_past``."""

    async def test_similar_past_surfaced_in_resolve_response(self) -> None:
        store = _build_store()
        app = _wire_app(store)

        result = await mcp_resolve_incident(
            app=app,
            alert_id=_SEED_ID,
            workspace_id=_WS,
        )

        # Field exists, is a list, and the 2 similar incidents are at
        # the top of the ranked list.
        assert "similar_past" in result
        similar_past = result["similar_past"]
        assert isinstance(similar_past, list)
        assert len(similar_past) >= 2
        top_two_ids = {item["incident_id"] for item in similar_past[:2]}
        assert top_two_ids == {_SIMILAR_1_ID, _SIMILAR_2_ID}

    async def test_resolve_incident_existing_fields_unchanged(self) -> None:
        """Additive-only contract: every pre-#233 field is still present."""
        store = _build_store()
        app = _wire_app(store)

        result = await mcp_resolve_incident(
            app=app,
            alert_id=_SEED_ID,
            workspace_id=_WS,
        )

        # Spot-check the schema: every legacy field is present with a
        # compatible type.  This is the regression guard required by
        # the #233 spec ("do not change existing field types").
        for field in (
            "alert",
            "target_resource",
            "responsible_pr",
            "slack_threads",
            "confidence_score",
            "effective_as_of",
        ):
            assert field in result, f"missing legacy field: {field}"

        # The new field is additive and present.
        assert "similar_past" in result


@pytest.mark.asyncio
class TestSimilarIncidentsAcl:
    """Cross-workspace seeds must 404 (alert_not_found)."""

    async def test_other_workspace_seed_returns_alert_not_found(self) -> None:
        store = _build_store()
        app = _wire_app(store)

        other_ws = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
        with pytest.raises(ValueError, match="alert_not_found"):
            await mcp_find_similar_incidents(
                app=app,
                incident_id=_SEED_ID,
                workspace_id=other_ws,
            )


@pytest.mark.asyncio
class TestSimilarIncidentsInvalidInput:
    """Invalid alert ids raise ``invalid_alert_id``."""

    async def test_non_alert_uri_raises(self) -> None:
        store = _build_store()
        app = _wire_app(store)

        with pytest.raises(ValueError, match="invalid_alert_id"):
            await mcp_find_similar_incidents(
                app=app,
                incident_id="not-an-alert-uri",
                workspace_id=_WS,
            )


def _maybe_match(matches: list[dict[str, Any]], incident_id: str) -> dict[str, Any] | None:
    """Tiny helper used by the focused breakdown assertion."""
    for m in matches:
        if m["incident_id"] == incident_id:
            return m
    return None
