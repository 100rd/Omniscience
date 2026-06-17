"""Contract tests for the ``resolve_incident`` MCP / REST tool (issue #153).

What this file covers
---------------------

1. Happy path: alert with full chain (resource -> PR -> Slack thread)
   returns a populated bundle with ``confidence_score`` 0.9.
2. ``alert_not_found`` -> ``ValueError("alert_not_found:...")`` from
   the MCP path / HTTP 404 from the REST path.
3. Cross-workspace ACL: alert in workspace A unreachable from workspace
   B's token, even with the full alert_id.  Returns ``alert_not_found``
   (404 not 403) — the documented "no existence leak" trade-off.
4. Empty graph (alert exists, no related entities) -> bundle with
   alert + empty fields, ``confidence_score`` 0.1.
5. ``as_of=T`` exercises the bitemporal path — ``GraphStore`` calls
   carry the supplied ``as_of`` verbatim.
6. Confidence-score heuristic exercised at all four boundaries
   (0.9, 0.6, 0.4, 0.1).
7. Invalid ``alert_id`` -> ``ValueError("invalid_alert_id:...")`` /
   HTTP 400 ``invalid_alert_id``.
8. MCP / REST parity: same alert returns the same JSON shape via both
   transports.

The fake ``GraphStore`` mirrors the bitemporal contract from
``tests/test_mcp_as_of.py`` but adds named neighbours so the
``find_related`` traversal returns realistic resource / PR / Slack
entities.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from omniscience_core.auth.tokens import generate_token, hash_token
from omniscience_core.db.models import ApiToken
from omniscience_core.storage.graph import (
    EntityNodeView,
    GraphEdgeView,
    GraphResultView,
)
from omniscience_server.app import create_app
from omniscience_server.incidents import (
    ALERT_NOT_FOUND_CODE,
    CONFIDENCE_ALERT_ONLY,
    CONFIDENCE_PR_NO_TEMPORAL_MATCH,
    CONFIDENCE_RESOURCE_ONLY,
    DEFAULT_MAX_DEPTH,
    INVALID_ALERT_ID_CODE,
    PR_RECENCY_WINDOW_SECONDS,
    mcp_resolve_incident,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WS_A = uuid.UUID("aaaaaaaa-1111-2222-3333-4444aa00153a")
_WS_B = uuid.UUID("bbbbbbbb-1111-2222-3333-4444bb00153b")

_ALERT_ID = "alert://pagerduty/INC-123"
_PR_URL = "https://github.com/acme/api/pull/42"
_SLACK_THREAD = "slack://channel/C0001/thread/1700000000.000100"
_TARGET_POD = "pod/api-7f9b"
_TARGET_ARN = "arn:aws:ec2:us-east-1:123456789012:instance/i-0abc"

_ALERT_FIRED_AT = datetime(2026, 4, 12, 19, 30, 0, tzinfo=UTC)
_PR_MERGED_RECENT = _ALERT_FIRED_AT - timedelta(hours=2)  # within 24h window
_PR_MERGED_OLD = _ALERT_FIRED_AT - timedelta(days=7)  # outside window

_AS_OF_T = datetime(2026, 4, 12, 19, 25, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fake graph store with named-neighbour support
# ---------------------------------------------------------------------------


class _IncidentGraphStore:
    """Workspace-scoped fake honouring ``(workspace_id, name, as_of)``.

    Stores per-workspace ``EntityNodeView`` instances by canonical name
    plus a per-workspace neighbour map for ``find_related`` traversal.
    Cross-workspace lookups return ``None`` (entity_not_found) per the
    protocol contract.
    """

    def __init__(self) -> None:
        self._entities: dict[tuple[uuid.UUID, str], EntityNodeView] = {}
        self._neighbours: dict[
            tuple[uuid.UUID, str],
            list[EntityNodeView],
        ] = {}
        self._edges: dict[
            tuple[uuid.UUID, str],
            list[GraphEdgeView],
        ] = {}
        self.calls: list[dict[str, Any]] = []

    def plant_entity(
        self,
        *,
        workspace_id: uuid.UUID,
        node: EntityNodeView,
    ) -> None:
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
            }
        )
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
            max_depth=max_depth,
            edge_types=edge_types,
        )


def _alert_node(*, name: str = _ALERT_ID, fired_at: datetime = _ALERT_FIRED_AT) -> EntityNodeView:
    return EntityNodeView(
        id=uuid.uuid4(),
        name=name,
        kind="alert",
        source="src-alerts",
        chunk_text=f"Alert {name}",
        depth=0,
        edge_type=None,
        valid_from=fired_at,
    )


def _pr_node(
    *,
    url: str = _PR_URL,
    merged_at: datetime | None = _PR_MERGED_RECENT,
    depth: int = 2,
    edge_type: str = "DEPLOYED_BY",
) -> EntityNodeView:
    return EntityNodeView(
        id=uuid.uuid4(),
        name=url,
        kind="pull_request",
        source="src-github",
        chunk_text=f"PR {url}",
        depth=depth,
        edge_type=edge_type,
        valid_from=merged_at,
    )


def _slack_node(thread: str = _SLACK_THREAD, depth: int = 2) -> EntityNodeView:
    return EntityNodeView(
        id=uuid.uuid4(),
        name=thread,
        kind="slack_thread",
        source="src-slack",
        chunk_text="...thread excerpt...",
        depth=depth,
        edge_type="DISCUSSED_IN",
    )


def _resource_node(
    *,
    name: str = _TARGET_POD,
    depth: int = 1,
    edge_type: str = "FIRES_AGAINST",
) -> EntityNodeView:
    return EntityNodeView(
        id=uuid.uuid4(),
        name=name,
        kind="cross_ref_target",
        source="src-alerts",
        chunk_text=None,
        depth=depth,
        edge_type=edge_type,
    )


def _seed_full_chain(
    store: _IncidentGraphStore,
    *,
    workspace_id: uuid.UUID,
    pr_merged_at: datetime | None = _PR_MERGED_RECENT,
) -> None:
    """Plant a full alert -> resource -> PR + slack thread chain."""
    alert = _alert_node()
    store.plant_entity(workspace_id=workspace_id, node=alert)
    pod = _resource_node()
    pr = _pr_node(merged_at=pr_merged_at)
    thread = _slack_node()
    store.plant_neighbours(
        workspace_id=workspace_id,
        seed_name=alert.name,
        related=[pod, pr, thread],
        edges=[
            GraphEdgeView(from_entity=alert.name, to_entity=pod.name, edge_type="FIRES_AGAINST"),
            GraphEdgeView(from_entity=pod.name, to_entity=pr.name, edge_type="DEPLOYED_BY"),
            GraphEdgeView(
                from_entity=alert.name,
                to_entity=thread.name,
                edge_type="DISCUSSED_IN",
            ),
        ],
    )


def _wire_session_factory(app: FastAPI) -> None:
    if getattr(app.state, "db_session_factory", None) is not None:
        return
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    app.state.db_session_factory = factory


def _wire_graph_store(app: FastAPI, store: _IncidentGraphStore) -> None:
    app.state.graph_store = store
    _wire_session_factory(app)


# ---------------------------------------------------------------------------
# 1. Direct tool happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestResolveIncidentHappyPath:
    """Full chain returns a populated bundle with high confidence."""

    async def test_full_chain_returns_alert_resource_pr_threads(self) -> None:
        store = _IncidentGraphStore()
        _seed_full_chain(store, workspace_id=_WS_A)
        app = FastAPI()
        _wire_graph_store(app, store)

        result = await mcp_resolve_incident(
            app=app,
            alert_id=_ALERT_ID,
            workspace_id=_WS_A,
        )

        assert result["alert"]["name"] == _ALERT_ID
        assert result["target_resource"]["name"] == _TARGET_POD
        assert result["target_resource"]["edge_type"] == "FIRES_AGAINST"
        assert result["responsible_pr"]["name"] == _PR_URL
        assert (
            datetime.fromisoformat(result["responsible_pr"]["merged_at"].replace("Z", "+00:00"))
            == _PR_MERGED_RECENT
        )
        assert len(result["slack_threads"]) == 1
        assert result["slack_threads"][0]["name"] == _SLACK_THREAD
        assert result["confidence_score"] == pytest.approx(0.6 + 0.3 * (2 ** (-1.0 / 6.0)))
        assert "effective_as_of" in result

    async def test_default_max_depth_forwarded(self) -> None:
        store = _IncidentGraphStore()
        _seed_full_chain(store, workspace_id=_WS_A)
        app = FastAPI()
        _wire_graph_store(app, store)

        await mcp_resolve_incident(app=app, alert_id=_ALERT_ID, workspace_id=_WS_A)

        find = next(c for c in store.calls if c["op"] == "find_related")
        assert find["max_depth"] == DEFAULT_MAX_DEPTH


# ---------------------------------------------------------------------------
# 2. alert_not_found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAlertNotFound:
    async def test_missing_alert_raises_alert_not_found(self) -> None:
        store = _IncidentGraphStore()
        app = FastAPI()
        _wire_graph_store(app, store)

        with pytest.raises(ValueError, match=ALERT_NOT_FOUND_CODE):
            await mcp_resolve_incident(app=app, alert_id=_ALERT_ID, workspace_id=_WS_A)


# ---------------------------------------------------------------------------
# 3. Cross-workspace ACL — MANDATORY contract test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestCrossWorkspaceIsolation:
    """Alert in workspace A must be unreachable from workspace B's token.

    Even with the exact alert_id, the lookup must return ``alert_not_found``.
    The 404 (vs 403) is intentional — see issue #153 §D and PR description.
    """

    async def test_workspace_b_cannot_read_workspace_a_alert(self) -> None:
        store = _IncidentGraphStore()
        _seed_full_chain(store, workspace_id=_WS_A)
        app = FastAPI()
        _wire_graph_store(app, store)

        with pytest.raises(ValueError, match=ALERT_NOT_FOUND_CODE):
            await mcp_resolve_incident(
                app=app,
                alert_id=_ALERT_ID,
                workspace_id=_WS_B,
            )

        # And the store saw only workspace B in any get_entity / find_related
        # call — no silent widening.
        for call in store.calls:
            assert call["workspace_id"] == _WS_B

    async def test_workspace_a_succeeds_workspace_b_fails_same_alert(self) -> None:
        """Symmetric: same alert_id resolves only for the planting workspace."""
        store = _IncidentGraphStore()
        _seed_full_chain(store, workspace_id=_WS_A)
        app = FastAPI()
        _wire_graph_store(app, store)

        # A succeeds.
        result_a = await mcp_resolve_incident(app=app, alert_id=_ALERT_ID, workspace_id=_WS_A)
        assert result_a["alert"]["name"] == _ALERT_ID

        # B fails with alert_not_found.
        with pytest.raises(ValueError, match=ALERT_NOT_FOUND_CODE):
            await mcp_resolve_incident(app=app, alert_id=_ALERT_ID, workspace_id=_WS_B)


# ---------------------------------------------------------------------------
# 4. Empty graph — alert exists but no edges
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestEmptyGraph:
    async def test_alert_with_no_neighbours_returns_alert_only(self) -> None:
        store = _IncidentGraphStore()
        store.plant_entity(workspace_id=_WS_A, node=_alert_node())
        # No neighbours planted -> empty list.
        store.plant_neighbours(workspace_id=_WS_A, seed_name=_ALERT_ID, related=[])
        app = FastAPI()
        _wire_graph_store(app, store)

        result = await mcp_resolve_incident(app=app, alert_id=_ALERT_ID, workspace_id=_WS_A)

        assert result["alert"]["name"] == _ALERT_ID
        assert result["target_resource"] is None
        assert result["responsible_pr"] is None
        assert result["slack_threads"] == []
        assert result["confidence_score"] == CONFIDENCE_ALERT_ONLY


# ---------------------------------------------------------------------------
# 5. as_of=T bitemporal path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAsOfPlumbing:
    """``as_of=T`` is forwarded to every store call verbatim."""

    async def test_as_of_forwarded_to_get_entity_and_find_related(self) -> None:
        store = _IncidentGraphStore()
        _seed_full_chain(store, workspace_id=_WS_A)
        app = FastAPI()
        _wire_graph_store(app, store)

        result = await mcp_resolve_incident(
            app=app,
            alert_id=_ALERT_ID,
            workspace_id=_WS_A,
            as_of=_AS_OF_T,
        )

        # effective_as_of echoes back.
        assert datetime.fromisoformat(result["effective_as_of"].replace("Z", "+00:00")) == _AS_OF_T

        # Both store ops carried as_of.
        get_call = next(c for c in store.calls if c["op"] == "get_entity")
        find_call = next(c for c in store.calls if c["op"] == "find_related")
        assert get_call["as_of"] == _AS_OF_T
        assert find_call["as_of"] == _AS_OF_T

    async def test_naive_as_of_rejected_with_invalid_timezone(self) -> None:
        """Naive ``as_of`` is rejected at the validation boundary."""
        store = _IncidentGraphStore()
        _seed_full_chain(store, workspace_id=_WS_A)
        app = FastAPI()
        _wire_graph_store(app, store)

        with pytest.raises(ValueError, match="invalid_timezone"):
            await mcp_resolve_incident(
                app=app,
                alert_id=_ALERT_ID,
                workspace_id=_WS_A,
                as_of=datetime(2026, 4, 12, 19, 25, 0),  # naive
            )


# ---------------------------------------------------------------------------
# 6. Confidence-score heuristic — 4 boundaries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestConfidenceScoreHeuristic:
    """Exercise the v0.1 deterministic placeholder at each rung."""

    async def test_pr_within_24h_returns_0_9(self) -> None:
        store = _IncidentGraphStore()
        _seed_full_chain(store, workspace_id=_WS_A, pr_merged_at=_PR_MERGED_RECENT)
        app = FastAPI()
        _wire_graph_store(app, store)

        result = await mcp_resolve_incident(app=app, alert_id=_ALERT_ID, workspace_id=_WS_A)
        assert result["confidence_score"] == pytest.approx(0.6 + 0.3 * (2 ** (-1.0 / 6.0)))

    async def test_pr_outside_window_returns_0_6(self) -> None:
        store = _IncidentGraphStore()
        _seed_full_chain(store, workspace_id=_WS_A, pr_merged_at=_PR_MERGED_OLD)
        app = FastAPI()
        _wire_graph_store(app, store)

        result = await mcp_resolve_incident(app=app, alert_id=_ALERT_ID, workspace_id=_WS_A)
        assert result["confidence_score"] == pytest.approx(0.6 + 0.3 * (2 ** -14.0))

    async def test_pr_with_no_merge_timestamp_returns_0_6(self) -> None:
        """Missing merge timestamp -> neutral 0.5 probability (score 0.75)."""
        store = _IncidentGraphStore()
        _seed_full_chain(store, workspace_id=_WS_A, pr_merged_at=None)
        app = FastAPI()
        _wire_graph_store(app, store)

        result = await mcp_resolve_incident(app=app, alert_id=_ALERT_ID, workspace_id=_WS_A)
        assert result["confidence_score"] == pytest.approx(0.75)

    async def test_resource_only_no_pr_returns_0_4(self) -> None:
        """Resource resolved but no PR -> 0.4."""
        store = _IncidentGraphStore()
        store.plant_entity(workspace_id=_WS_A, node=_alert_node())
        store.plant_neighbours(
            workspace_id=_WS_A,
            seed_name=_ALERT_ID,
            related=[_resource_node()],
        )
        app = FastAPI()
        _wire_graph_store(app, store)

        result = await mcp_resolve_incident(app=app, alert_id=_ALERT_ID, workspace_id=_WS_A)
        assert result["confidence_score"] == CONFIDENCE_RESOURCE_ONLY
        assert result["target_resource"] is not None
        assert result["responsible_pr"] is None

    async def test_alert_only_returns_0_1(self) -> None:
        """Only the alert resolves -> 0.1 floor."""
        store = _IncidentGraphStore()
        store.plant_entity(workspace_id=_WS_A, node=_alert_node())
        store.plant_neighbours(workspace_id=_WS_A, seed_name=_ALERT_ID, related=[])
        app = FastAPI()
        _wire_graph_store(app, store)

        result = await mcp_resolve_incident(app=app, alert_id=_ALERT_ID, workspace_id=_WS_A)
        assert result["confidence_score"] == CONFIDENCE_ALERT_ONLY

    async def test_pr_merged_after_alert_is_not_temporal_match(self) -> None:
        """A PR merged AFTER the alert fired is not a cause."""
        store = _IncidentGraphStore()
        # Merge 1 hour AFTER alert fires.
        _seed_full_chain(
            store,
            workspace_id=_WS_A,
            pr_merged_at=_ALERT_FIRED_AT + timedelta(hours=1),
        )
        app = FastAPI()
        _wire_graph_store(app, store)

        result = await mcp_resolve_incident(app=app, alert_id=_ALERT_ID, workspace_id=_WS_A)
        assert result["confidence_score"] == CONFIDENCE_PR_NO_TEMPORAL_MATCH


# ---------------------------------------------------------------------------
# 7. Invalid alert_id
# ---------------------------------------------------------------------------


class TestAlertIdValidation:
    """``alert_id`` must be ``alert://{provider}/{provider_alert_id}``."""

    @pytest.mark.parametrize(
        "bad",
        [
            "INC-123",
            "https://example.com/INC-123",
            "alert://",
            "alert://only-one-segment",
            "alert:///missing-provider",
            "alert://provider/",
        ],
    )
    @pytest.mark.asyncio
    async def test_invalid_alert_id_rejected(self, bad: str) -> None:
        store = _IncidentGraphStore()
        app = FastAPI()
        _wire_graph_store(app, store)

        with pytest.raises(ValueError, match=INVALID_ALERT_ID_CODE):
            await mcp_resolve_incident(app=app, alert_id=bad, workspace_id=_WS_A)


# ---------------------------------------------------------------------------
# 8. REST surface — auth + parity with MCP
# ---------------------------------------------------------------------------


def _auth_token(workspace_id: uuid.UUID | None) -> tuple[MagicMock, str]:
    plaintext, prefix = generate_token("ri")
    token: MagicMock = MagicMock(spec=ApiToken)
    token.hashed_token = hash_token(plaintext)
    token.token_prefix = prefix
    token.scopes = ["search"]
    token.expires_at = None
    token.is_active = True
    token.workspace_id = workspace_id
    return token, plaintext


def _make_rest_app() -> FastAPI:
    from omniscience_core.config import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://test:test@localhost:5432/test",
        nats_url="nats://localhost:4222",
        log_level="WARNING",
        otlp_endpoint=None,
        environment="test",
    )
    return create_app(settings=settings)


@pytest.mark.asyncio
class TestRestResolveIncident:
    """REST mirror — same JSON shape, workspace-scoped, fail-closed."""

    async def test_happy_path_post(self) -> None:
        app = _make_rest_app()
        store = _IncidentGraphStore()
        _seed_full_chain(store, workspace_id=_WS_A)
        _wire_graph_store(app, store)
        token, plaintext = _auth_token(workspace_id=_WS_A)

        with patch(
            "omniscience_core.auth.middleware._lookup_token",
            new_callable=AsyncMock,
            return_value=token,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    f"/api/v1/incidents/{_ALERT_ID}/resolve",
                    headers={"Authorization": f"Bearer {plaintext}"},
                )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["alert"]["name"] == _ALERT_ID
        assert body["responsible_pr"]["name"] == _PR_URL
        assert body["confidence_score"] == pytest.approx(0.6 + 0.3 * (2 ** (-1.0 / 6.0)))

    async def test_alert_not_found_returns_404(self) -> None:
        app = _make_rest_app()
        store = _IncidentGraphStore()
        _wire_graph_store(app, store)
        token, plaintext = _auth_token(workspace_id=_WS_A)

        with patch(
            "omniscience_core.auth.middleware._lookup_token",
            new_callable=AsyncMock,
            return_value=token,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    f"/api/v1/incidents/{_ALERT_ID}/resolve",
                    headers={"Authorization": f"Bearer {plaintext}"},
                )
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == ALERT_NOT_FOUND_CODE

    async def test_workspace_mismatch_returns_404_not_403(self) -> None:
        """Cross-workspace ACL: foreign alert_id -> 404 (no existence leak).

        See issue #153 §D and PR description: 404 over 403 is intentional.
        """
        app = _make_rest_app()
        store = _IncidentGraphStore()
        _seed_full_chain(store, workspace_id=_WS_A)
        _wire_graph_store(app, store)
        token, plaintext = _auth_token(workspace_id=_WS_B)

        with patch(
            "omniscience_core.auth.middleware._lookup_token",
            new_callable=AsyncMock,
            return_value=token,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    f"/api/v1/incidents/{_ALERT_ID}/resolve",
                    headers={"Authorization": f"Bearer {plaintext}"},
                )
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == ALERT_NOT_FOUND_CODE

    async def test_invalid_alert_id_returns_400(self) -> None:
        app = _make_rest_app()
        store = _IncidentGraphStore()
        _wire_graph_store(app, store)
        token, plaintext = _auth_token(workspace_id=_WS_A)

        with patch(
            "omniscience_core.auth.middleware._lookup_token",
            new_callable=AsyncMock,
            return_value=token,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/incidents/INC-123/resolve",
                    headers={"Authorization": f"Bearer {plaintext}"},
                )
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == INVALID_ALERT_ID_CODE

    async def test_unscoped_token_rejected_403(self) -> None:
        app = _make_rest_app()
        store = _IncidentGraphStore()
        _seed_full_chain(store, workspace_id=_WS_A)
        _wire_graph_store(app, store)
        token, plaintext = _auth_token(workspace_id=None)

        with patch(
            "omniscience_core.auth.middleware._lookup_token",
            new_callable=AsyncMock,
            return_value=token,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    f"/api/v1/incidents/{_ALERT_ID}/resolve",
                    headers={"Authorization": f"Bearer {plaintext}"},
                )
        assert resp.status_code == 403, resp.text

    async def test_as_of_query_param_forwarded(self) -> None:
        app = _make_rest_app()
        store = _IncidentGraphStore()
        _seed_full_chain(store, workspace_id=_WS_A)
        _wire_graph_store(app, store)
        token, plaintext = _auth_token(workspace_id=_WS_A)

        with patch(
            "omniscience_core.auth.middleware._lookup_token",
            new_callable=AsyncMock,
            return_value=token,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    f"/api/v1/incidents/{_ALERT_ID}/resolve",
                    params={"as_of": _AS_OF_T.isoformat()},
                    headers={"Authorization": f"Bearer {plaintext}"},
                )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert datetime.fromisoformat(body["effective_as_of"].replace("Z", "+00:00")) == _AS_OF_T

        find_call = next(c for c in store.calls if c["op"] == "find_related")
        assert find_call["as_of"] == _AS_OF_T

    async def test_naive_as_of_returns_400(self) -> None:
        app = _make_rest_app()
        store = _IncidentGraphStore()
        _seed_full_chain(store, workspace_id=_WS_A)
        _wire_graph_store(app, store)
        token, plaintext = _auth_token(workspace_id=_WS_A)

        with patch(
            "omniscience_core.auth.middleware._lookup_token",
            new_callable=AsyncMock,
            return_value=token,
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    f"/api/v1/incidents/{_ALERT_ID}/resolve",
                    params={"as_of": "2026-04-12T19:25:00"},
                    headers={"Authorization": f"Bearer {plaintext}"},
                )
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "invalid_timezone"


# ---------------------------------------------------------------------------
# 9. PR_RECENCY_WINDOW_SECONDS boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRecencyBoundary:
    async def test_exactly_at_window_edge_is_temporal_match(self) -> None:
        """At exactly ``PR_RECENCY_WINDOW_SECONDS`` (two half-lives) before alert."""
        store = _IncidentGraphStore()
        edge = _ALERT_FIRED_AT - timedelta(seconds=PR_RECENCY_WINDOW_SECONDS)
        _seed_full_chain(store, workspace_id=_WS_A, pr_merged_at=edge)
        app = FastAPI()
        _wire_graph_store(app, store)

        result = await mcp_resolve_incident(app=app, alert_id=_ALERT_ID, workspace_id=_WS_A)
        assert result["confidence_score"] == pytest.approx(0.6 + 0.3 * (2 ** -2.0))

    async def test_one_second_outside_window_is_no_match(self) -> None:
        store = _IncidentGraphStore()
        outside = _ALERT_FIRED_AT - timedelta(seconds=PR_RECENCY_WINDOW_SECONDS + 1)
        _seed_full_chain(store, workspace_id=_WS_A, pr_merged_at=outside)
        app = FastAPI()
        _wire_graph_store(app, store)

        result = await mcp_resolve_incident(app=app, alert_id=_ALERT_ID, workspace_id=_WS_A)
        assert result["confidence_score"] == pytest.approx(0.6 + 0.3 * (2 ** (-86401.0 / 43200.0)))
