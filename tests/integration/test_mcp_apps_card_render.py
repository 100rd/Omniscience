"""End-to-end integration test for the MCP Apps card (issue #238).

Exercises the **registered MCP tool** (not just the pure builder) by
driving it through a FastMCP ``Context`` whose ``session.client_params``
mirrors the wire-level shape an MCP client would send on initialize.
Verifies:

* When the client advertises ``omniscience/apps``, the tool response
  carries the rendered card under ``_meta["omniscience/apps"]``,
  conforming to :class:`ResolveIncidentCard`.
* When the client does NOT advertise the capability, the response is
  the legacy dict, byte-for-byte (no ``_meta`` key).
* The card validates against the published schema (``model_validate``).
* The card carries the expected sections derived from the bundled
  fixture (alert headline, three candidates ranked by confidence,
  citations on each candidate).
* The optional ``similar_past`` block is rendered when the inner
  response carries it (forward-compat with #233).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from omniscience_core.auth.scopes import Scope
from omniscience_core.auth.tokens import generate_token, hash_token
from omniscience_core.db.models import ApiToken
from omniscience_core.storage.graph import (
    EntityNodeView,
    GraphEdgeView,
    GraphResultView,
)
from omniscience_server.mcp.apps import (
    APPS_CAPABILITY_KEY,
    ResolveIncidentCard,
)

# ---------------------------------------------------------------------------
# Fixture data — a single workspace, one fully-resolvable alert
# ---------------------------------------------------------------------------

_WS = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ALERT_ID = "alert://pagerduty/INC-9001"
_ALERT_FIRED_AT = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
_PR_MERGED_AT = datetime(2026, 5, 22, 11, 30, 0, tzinfo=UTC)
_PR_URI = "https://github.com/acme/api/pull/42"
_RESOURCE = "default/api"
_SLACK_THREAD = "slack://channel/C12345/thread/1716383700.000100"


def _alert_node() -> EntityNodeView:
    return EntityNodeView(
        id=uuid.uuid4(),
        name=_ALERT_ID,
        kind="alert",
        source="pagerduty",
        chunk_text="API 500s spiking on /v1/widgets",
        depth=0,
        edge_type=None,
        valid_from=_ALERT_FIRED_AT,
    )


def _pr_node() -> EntityNodeView:
    return EntityNodeView(
        id=uuid.uuid4(),
        name=_PR_URI,
        kind="pull_request",
        source="github",
        chunk_text=None,
        depth=2,
        edge_type="deployed_to",
        valid_from=_PR_MERGED_AT,
    )


def _resource_node() -> EntityNodeView:
    return EntityNodeView(
        id=uuid.uuid4(),
        name=_RESOURCE,
        kind="k8s_deployment",
        source="kubernetes",
        chunk_text=None,
        depth=1,
        edge_type="alerts_on",
    )


def _slack_node() -> EntityNodeView:
    return EntityNodeView(
        id=uuid.uuid4(),
        name=_SLACK_THREAD,
        kind="slack_thread",
        source="slack",
        chunk_text="@oncall I'm seeing the same",
        depth=2,
        edge_type="discusses",
    )


class _FakeGraphStore:
    """In-process workspace-scoped store wired for the integration test."""

    def __init__(self) -> None:
        self.alert = _alert_node()
        self.neighbours = [_resource_node(), _pr_node(), _slack_node()]

    async def get_entity(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
    ) -> EntityNodeView | None:
        if workspace_id != _WS:
            return None
        if entity_name == _ALERT_ID:
            return self.alert
        for n in self.neighbours:
            if n.name == entity_name:
                return n
        return None

    async def find_related(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
        max_depth: int = 1,
        edge_types: list[str] | None = None,
    ) -> GraphResultView:
        if workspace_id != _WS or entity_name != _ALERT_ID:
            raise ValueError(f"entity_not_found:{entity_name}")
        return GraphResultView(
            seed=self.alert,
            related=list(self.neighbours),
            edges=[
                GraphEdgeView(from_entity=_ALERT_ID, to_entity=_RESOURCE, edge_type="alerts_on"),
                GraphEdgeView(from_entity=_RESOURCE, to_entity=_PR_URI, edge_type="deployed_to"),
                GraphEdgeView(
                    from_entity=_RESOURCE, to_entity=_SLACK_THREAD, edge_type="discusses"
                ),
            ],
        )


# ---------------------------------------------------------------------------
# Auth helpers — mirror the pattern in tests/test_mcp_resolve_incident.py
# ---------------------------------------------------------------------------


def _auth_token(workspace_id: uuid.UUID | None) -> tuple[MagicMock, str]:
    """Build a MagicMock ``ApiToken`` plus its plaintext.

    Mirrors the helper in ``tests/test_mcp_resolve_incident.py`` so the
    auth surface behaves identically.  The ``_resolve_token`` patch in
    the test bodies bypasses the actual lookup, but we still build a
    realistic token so any downstream telemetry / logging that reads
    ``token_prefix`` does not blow up.
    """
    plaintext, prefix = generate_token("ai")
    token: MagicMock = MagicMock(spec=ApiToken)
    token.id = uuid.uuid4()
    token.workspace_id = workspace_id
    token.scopes = [Scope.search.value]
    token.token_prefix = prefix
    token.hashed_token = hash_token(plaintext)
    token.expires_at = None
    token.is_active = True
    return token, plaintext


def _build_app() -> tuple[FastAPI, _FakeGraphStore]:
    """Stand up a FastAPI app wired with the fake graph store + dummy session factory."""
    app = FastAPI()
    store = _FakeGraphStore()
    app.state.graph_store = store
    app.state.db_session_factory = AsyncMock()
    return app, store


def _make_ctx(
    *,
    supports_apps: bool,
    token: MagicMock,
) -> Any:
    """Build a fake FastMCP ``Context`` carrying the bearer token + caps.

    The structure mirrors what the real FastMCP runtime provides:

    * ``ctx.session.client_params.capabilities.experimental`` — read by
      :func:`should_emit_card`.
    * ``ctx._request_context.request.headers`` — read by
      :func:`_extract_bearer` so the auth path resolves the token.
    """
    capabilities: dict[str, Any] = {}
    if supports_apps:
        capabilities = {"experimental": {APPS_CAPABILITY_KEY: {}}}
    client_params = SimpleNamespace(capabilities=capabilities)
    session = SimpleNamespace(client_params=client_params)

    headers = {"authorization": "Bearer dummy"}
    request = SimpleNamespace(headers=headers)
    request_context = SimpleNamespace(request=request)

    return SimpleNamespace(
        session=session,
        _request_context=request_context,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMcpAppsCardRender:
    """Integration: the registered ``resolve_incident`` tool yields a card
    when the client advertises ``omniscience/apps`` and a plain legacy
    response otherwise."""

    async def test_apps_client_receives_card_under_meta(self) -> None:
        """Happy path: capability advertised -> card attached."""
        app, _ = _build_app()
        token, _ = _auth_token(_WS)
        ctx = _make_ctx(supports_apps=True, token=token)

        with (
            patch(
                "omniscience_server.mcp.server._resolve_token",
                new=AsyncMock(return_value=token),
            ),
            patch(
                "omniscience_server.mcp.server._get_app",
                return_value=app,
            ),
        ):
            # Use ``.fn`` to bypass FastMCP's wire-validation layer and
            # invoke the tool body directly with our crafted Context.
            from omniscience_server.mcp.server import resolve_incident

            tool_fn = getattr(resolve_incident, "fn", resolve_incident)
            response = await tool_fn(alert_id=_ALERT_ID, ctx=ctx)

        # Card is present under the MCP-standard _meta slot.
        assert "_meta" in response
        assert APPS_CAPABILITY_KEY in response["_meta"]
        raw_card = response["_meta"][APPS_CAPABILITY_KEY]

        # Validates against the published schema.
        card = ResolveIncidentCard.model_validate(raw_card)
        assert card.apps_version == "1.0"
        assert card.card_type == "resolve_incident"

        # Summary section
        assert card.summary.alert_uri == _ALERT_ID
        assert card.summary.headline == "API 500s spiking on /v1/widgets"
        assert card.summary.below_trust_threshold is False

        # Three candidates: PR, resource, slack thread
        assert len(card.candidates) == 3
        kinds = [c.kind for c in card.candidates]
        assert kinds == ["pull_request", "resource", "slack_thread"]

        # Each candidate has at least one citation
        for cand in card.candidates:
            assert len(cand.citations) >= 1
            assert cand.citations[0].uri

        # Confidence bars in descending order (PR > resource > thread)
        scores = [c.confidence.value for c in card.candidates]
        assert scores[0] >= scores[1] >= scores[2]

        # Legacy fields are still present at the top level for clients
        # that ignore _meta.
        assert response["alert"]["name"] == _ALERT_ID
        assert response["responsible_pr"]["name"] == _PR_URI
        assert response["target_resource"]["name"] == _RESOURCE
        assert response["confidence_score"] == pytest.approx(0.8914595823460818)

    async def test_non_apps_client_receives_legacy_only(self) -> None:
        """Backwards-compat: no capability -> no card."""
        app, _ = _build_app()
        token, _ = _auth_token(_WS)
        ctx = _make_ctx(supports_apps=False, token=token)

        with (
            patch(
                "omniscience_server.mcp.server._resolve_token",
                new=AsyncMock(return_value=token),
            ),
            patch(
                "omniscience_server.mcp.server._get_app",
                return_value=app,
            ),
        ):
            from omniscience_server.mcp.server import resolve_incident

            tool_fn = getattr(resolve_incident, "fn", resolve_incident)
            response = await tool_fn(alert_id=_ALERT_ID, ctx=ctx)

        # No _meta key at all — legacy clients see the unchanged shape.
        assert "_meta" not in response
        # Top-level legacy fields untouched.
        assert response["alert"]["name"] == _ALERT_ID
        assert response["confidence_score"] == pytest.approx(0.8914595823460818)

    async def test_card_schema_round_trips_through_json(self) -> None:
        """The card payload must survive a JSON round-trip unchanged."""
        import json as _json

        app, _ = _build_app()
        token, _ = _auth_token(_WS)
        ctx = _make_ctx(supports_apps=True, token=token)

        with (
            patch(
                "omniscience_server.mcp.server._resolve_token",
                new=AsyncMock(return_value=token),
            ),
            patch(
                "omniscience_server.mcp.server._get_app",
                return_value=app,
            ),
        ):
            from omniscience_server.mcp.server import resolve_incident

            tool_fn = getattr(resolve_incident, "fn", resolve_incident)
            response = await tool_fn(alert_id=_ALERT_ID, ctx=ctx)

        raw = response["_meta"][APPS_CAPABILITY_KEY]
        serialised = _json.dumps(raw)
        reparsed = _json.loads(serialised)
        ResolveIncidentCard.model_validate(reparsed)  # must not raise

    async def test_similar_past_field_renders_when_inner_response_carries_it(
        self,
    ) -> None:
        """Forward-compat with #233 — inner response may add ``similar_past``."""
        app, _ = _build_app()
        token, _ = _auth_token(_WS)
        ctx = _make_ctx(supports_apps=True, token=token)

        # Simulate #233's parallel work by patching the inner tool to
        # return a legacy response with similar_past attached.
        async def _fake_inner(**_kw: Any) -> dict[str, Any]:
            from omniscience_server.incidents import mcp_resolve_incident as _real

            base = await _real(**_kw)
            base["similar_past"] = [
                {
                    "alert_id": "alert://pagerduty/INC-1000",
                    "title": "Earlier API 500s spike",
                    "occurred_at": "2026-05-15T12:00:00+00:00",
                    "similarity_score": 0.87,
                    "resolution_hint": "Rolled back PR #41",
                }
            ]
            return base

        with (
            patch(
                "omniscience_server.mcp.server._resolve_token",
                new=AsyncMock(return_value=token),
            ),
            patch(
                "omniscience_server.mcp.server._get_app",
                return_value=app,
            ),
            patch(
                "omniscience_server.mcp.server.mcp_resolve_incident",
                new=_fake_inner,
            ),
        ):
            from omniscience_server.mcp.server import resolve_incident

            tool_fn = getattr(resolve_incident, "fn", resolve_incident)
            response = await tool_fn(alert_id=_ALERT_ID, ctx=ctx)

        card = ResolveIncidentCard.model_validate(response["_meta"][APPS_CAPABILITY_KEY])
        assert len(card.similar_past) == 1
        assert card.similar_past[0].alert_id == "alert://pagerduty/INC-1000"
        assert card.similar_past[0].similarity_score == 0.87

    async def test_apps_client_response_preserves_all_legacy_keys(self) -> None:
        """Every legacy key MUST be present at the top level even when the
        card is attached — this is the contract that lets the same wire
        format serve both legacy and Apps-aware clients simultaneously."""
        app, _ = _build_app()
        token, _ = _auth_token(_WS)
        legacy_ctx = _make_ctx(supports_apps=False, token=token)
        apps_ctx = _make_ctx(supports_apps=True, token=token)

        with (
            patch(
                "omniscience_server.mcp.server._resolve_token",
                new=AsyncMock(return_value=token),
            ),
            patch(
                "omniscience_server.mcp.server._get_app",
                return_value=app,
            ),
        ):
            from omniscience_server.mcp.server import resolve_incident

            tool_fn = getattr(resolve_incident, "fn", resolve_incident)
            legacy_response = await tool_fn(alert_id=_ALERT_ID, ctx=legacy_ctx)
            apps_response = await tool_fn(alert_id=_ALERT_ID, ctx=apps_ctx)

        # Every key in the legacy response is present in the Apps
        # response.  Values are compared field-by-field, skipping
        # ``effective_as_of`` which is wall-clock derived and will
        # naturally drift between the two calls — its presence + ISO-8601
        # shape is the contract, not equality.
        for key, value in legacy_response.items():
            assert key in apps_response
            if key == "effective_as_of":
                assert isinstance(apps_response[key], str)
                continue
            assert apps_response[key] == value
        # And the Apps response has exactly one extra key.
        extra = set(apps_response) - set(legacy_response)
        assert extra == {"_meta"}
