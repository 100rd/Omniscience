"""Integration tests for the incident-timeline endpoint (issue #235).

Reuses the ``tests/fixtures/incident_demo/workspace_a.json`` fixture
that the H5 demo path already depends on — workspace_a plants an alert
plus six neighbours (pod, two PRs, two Slack threads, one OTel trace),
each carrying a ``valid_from``.  That gives the timeline projection
six "created" events from neighbours plus one for the seed alert — well
above the issue's "5+ ordered events" acceptance bar.

What this exercises
-------------------

1. The full composition path ``alert_id`` -> ``GraphStore.find_related``
   -> bitemporal projection -> sorted ``TimelineEvent`` list.
2. The ``[from, to)`` event-window filter.
3. The ``entity_types`` allowlist filter.
4. Cross-workspace isolation (alert from workspace_b is ``not_found``
   when queried as workspace_a).
5. The Mermaid renderer produces a parseable ``timeline`` block whose
   sections and events line up with the JSON payload.
6. Provenance: every event carries the ``source`` field the connector
   wrote (ADR-0008 §1).

Runs in the default CI lane — no env-var opt-in, no testcontainers.
The in-memory store is the contract surface, matching
``test_incident_demo.py``'s posture.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from omniscience_core.storage.graph import (
    EntityNodeView,
    GraphEdgeView,
    GraphResultView,
)
from omniscience_server.incident_timeline import (
    MAX_EVENTS_RETURNED,
    mcp_incident_timeline,
    render_mermaid,
)
from omniscience_server.incidents import (
    ALERT_NOT_FOUND_CODE,
    INVALID_ALERT_ID_CODE,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FIXTURE_DIR: Path = Path(__file__).parent.parent / "fixtures" / "incident_demo"

#: Acceptance floor from issue #235: "fixture incident -> 5+ ordered events".
_MIN_EVENT_COUNT: int = 5


# ---------------------------------------------------------------------------
# Fixture / store helpers (mirror test_incident_demo.py)
# ---------------------------------------------------------------------------


def _load_fixture(filename: str) -> dict[str, Any]:
    return json.loads((_FIXTURE_DIR / filename).read_text(encoding="utf-8"))


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _node_from_dict(payload: dict[str, Any]) -> EntityNodeView:
    return EntityNodeView(
        name=payload["name"],
        kind=payload["kind"],
        source=payload["source"],
        chunk_text=payload.get("chunk_text"),
        depth=int(payload.get("depth", 0)),
        edge_type=payload.get("edge_type"),
        valid_from=_parse_dt(payload.get("valid_from")),
        valid_to=_parse_dt(payload.get("valid_to")),
        recorded_at=_parse_dt(payload.get("recorded_at")),
    )


def _edge_from_dict(payload: dict[str, Any]) -> GraphEdgeView:
    return GraphEdgeView(
        from_entity=payload["from_entity"],
        to_entity=payload["to_entity"],
        edge_type=payload["edge_type"],
    )


class _TimelineGraphStore:
    """In-memory ``GraphStore`` fake — same shape as ``_DemoGraphStore``.

    Stores entities and neighbour lists keyed by ``(workspace_id, name)``
    and honours the ACL invariant (cross-workspace lookups return
    ``None`` / ``entity_not_found``).
    """

    def __init__(self) -> None:
        self._entities: dict[tuple[uuid.UUID, str], EntityNodeView] = {}
        self._neighbours: dict[tuple[uuid.UUID, str], list[EntityNodeView]] = {}
        self._edges: dict[tuple[uuid.UUID, str], list[GraphEdgeView]] = {}

    def plant_workspace(self, payload: dict[str, Any]) -> None:
        workspace_id = uuid.UUID(payload["workspace_id"])
        alert = _node_from_dict(payload["alert"])
        self._entities[(workspace_id, alert.name)] = alert
        neighbours = [_node_from_dict(n) for n in payload["neighbours"]]
        for n in neighbours:
            self._entities[(workspace_id, n.name)] = n
        self._neighbours[(workspace_id, alert.name)] = neighbours
        self._edges[(workspace_id, alert.name)] = [_edge_from_dict(e) for e in payload["edges"]]

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


def _build_app() -> tuple[FastAPI, dict[str, Any], dict[str, Any]]:
    ws_a = _load_fixture("workspace_a.json")
    ws_b = _load_fixture("workspace_b.json")
    store = _TimelineGraphStore()
    store.plant_workspace(ws_a)
    store.plant_workspace(ws_b)
    app = FastAPI()
    app.state.graph_store = store
    return app, ws_a, ws_b


# ---------------------------------------------------------------------------
# 1. Acceptance: 5+ ordered events with provenance
# ---------------------------------------------------------------------------


class TestTimelineAcceptance:
    """Issue #235 acceptance: 5+ ordered events with correct provenance."""

    async def test_returns_at_least_five_events(self) -> None:
        app, ws_a, _ = _build_app()
        result = await mcp_incident_timeline(
            app=app,
            alert_id=ws_a["alert"]["name"],
            workspace_id=uuid.UUID(ws_a["workspace_id"]),
        )
        assert len(result["events"]) >= _MIN_EVENT_COUNT, (
            f"acceptance: fixture must produce >= {_MIN_EVENT_COUNT} events; "
            f"got {len(result['events'])}"
        )

    async def test_events_are_strictly_ordered_by_ts(self) -> None:
        app, ws_a, _ = _build_app()
        result = await mcp_incident_timeline(
            app=app,
            alert_id=ws_a["alert"]["name"],
            workspace_id=uuid.UUID(ws_a["workspace_id"]),
        )
        timestamps = [_parse_dt(e["ts"]) for e in result["events"]]
        for prev, curr in pairwise(timestamps):
            assert prev is not None and curr is not None
            assert prev <= curr, (
                f"events must be sorted ascending by ts; {prev.isoformat()} > {curr.isoformat()}"
            )

    async def test_every_event_has_provenance(self) -> None:
        """ADR-0008 §1: every event carries the ``source`` connector id."""
        app, ws_a, _ = _build_app()
        result = await mcp_incident_timeline(
            app=app,
            alert_id=ws_a["alert"]["name"],
            workspace_id=uuid.UUID(ws_a["workspace_id"]),
        )
        sources = [e["source"] for e in result["events"]]
        assert all(s for s in sources), "every event must carry a source"
        # Sanity: the fixture uses src-* prefixes; ensure those flow through.
        assert any(s.startswith("src-") for s in sources)

    async def test_event_shape_matches_acceptance_spec(self) -> None:
        """Each event has the seven fields the issue enumerates."""
        app, ws_a, _ = _build_app()
        result = await mcp_incident_timeline(
            app=app,
            alert_id=ws_a["alert"]["name"],
            workspace_id=uuid.UUID(ws_a["workspace_id"]),
        )
        required = {
            "ts",
            "entity_id",
            "entity_type",
            "change_kind",
            "before_state_summary",
            "after_state_summary",
            "source",
        }
        for event in result["events"]:
            assert required.issubset(event.keys()), (
                f"event missing required field(s): {required - set(event.keys())}"
            )

    async def test_seed_alert_is_in_the_timeline(self) -> None:
        """The seed alert's ``valid_from`` must project to a 'created' event."""
        app, ws_a, _ = _build_app()
        result = await mcp_incident_timeline(
            app=app,
            alert_id=ws_a["alert"]["name"],
            workspace_id=uuid.UUID(ws_a["workspace_id"]),
        )
        alert_events = [
            e
            for e in result["events"]
            if e["entity_id"] == ws_a["alert"]["name"] and e["change_kind"] == "created"
        ]
        assert len(alert_events) == 1, "seed alert must appear exactly once as 'created'"


# ---------------------------------------------------------------------------
# 2. Window & entity-type filters
# ---------------------------------------------------------------------------


class TestTimelineFilters:
    """``from`` / ``to`` / ``entity_types`` query-param semantics."""

    async def test_from_filter_drops_earlier_events(self) -> None:
        app, ws_a, _ = _build_app()
        # Cut at the alert's fire time — earlier "created" events are dropped.
        from_ts = _parse_dt(ws_a["alert_fired_at"])
        assert from_ts is not None
        result = await mcp_incident_timeline(
            app=app,
            alert_id=ws_a["alert"]["name"],
            workspace_id=uuid.UUID(ws_a["workspace_id"]),
            from_ts=from_ts,
        )
        for event in result["events"]:
            ts = _parse_dt(event["ts"])
            assert ts is not None and ts >= from_ts

    async def test_to_filter_drops_later_events(self) -> None:
        app, ws_a, _ = _build_app()
        # to is exclusive; pick a bound an hour before the alert fired.
        fired = _parse_dt(ws_a["alert_fired_at"])
        assert fired is not None
        to_ts = fired - timedelta(hours=1)
        result = await mcp_incident_timeline(
            app=app,
            alert_id=ws_a["alert"]["name"],
            workspace_id=uuid.UUID(ws_a["workspace_id"]),
            to_ts=to_ts,
        )
        for event in result["events"]:
            ts = _parse_dt(event["ts"])
            assert ts is not None and ts < to_ts

    async def test_entity_types_filter_drops_other_kinds(self) -> None:
        app, ws_a, _ = _build_app()
        result = await mcp_incident_timeline(
            app=app,
            alert_id=ws_a["alert"]["name"],
            workspace_id=uuid.UUID(ws_a["workspace_id"]),
            entity_types=["pull_request"],
        )
        kinds = {e["entity_type"] for e in result["events"]}
        assert kinds == {"pull_request"}, (
            f"entity_types allowlist must filter to only 'pull_request'; got {kinds}"
        )

    async def test_empty_filter_returns_empty_envelope(self) -> None:
        """A non-overlapping window returns zero events, not 404."""
        app, ws_a, _ = _build_app()
        far_future = datetime(2099, 1, 1, tzinfo=UTC)
        result = await mcp_incident_timeline(
            app=app,
            alert_id=ws_a["alert"]["name"],
            workspace_id=uuid.UUID(ws_a["workspace_id"]),
            from_ts=far_future,
        )
        assert result["events"] == []
        assert result["alert_id"] == ws_a["alert"]["name"]


# ---------------------------------------------------------------------------
# 3. Error contract (parity with resolve_incident)
# ---------------------------------------------------------------------------


class TestTimelineErrors:
    async def test_unknown_alert_raises_alert_not_found(self) -> None:
        app, ws_a, _ = _build_app()
        with pytest.raises(ValueError) as excinfo:
            await mcp_incident_timeline(
                app=app,
                alert_id="alert://pagerduty/does-not-exist",
                workspace_id=uuid.UUID(ws_a["workspace_id"]),
            )
        assert str(excinfo.value).startswith(f"{ALERT_NOT_FOUND_CODE}:")

    async def test_cross_workspace_alert_is_not_found(self) -> None:
        """workspace_a may NOT see workspace_b's alert."""
        app, ws_a, ws_b = _build_app()
        with pytest.raises(ValueError) as excinfo:
            await mcp_incident_timeline(
                app=app,
                alert_id=ws_b["alert"]["name"],
                workspace_id=uuid.UUID(ws_a["workspace_id"]),
            )
        assert str(excinfo.value).startswith(f"{ALERT_NOT_FOUND_CODE}:")

    async def test_malformed_alert_id_raises_invalid_alert_id(self) -> None:
        app, ws_a, _ = _build_app()
        with pytest.raises(ValueError) as excinfo:
            await mcp_incident_timeline(
                app=app,
                alert_id="not-an-alert-uri",
                workspace_id=uuid.UUID(ws_a["workspace_id"]),
            )
        assert str(excinfo.value).startswith(f"{INVALID_ALERT_ID_CODE}:")


# ---------------------------------------------------------------------------
# 4. Mermaid renderer
# ---------------------------------------------------------------------------


def _is_valid_mermaid_timeline(block: str) -> bool:
    """Cheap structural check — full Mermaid render is not required here.

    Verifies the block starts with ``timeline``, declares a ``title``,
    contains at least one ``section`` header, and that every non-blank
    body line is indented (Mermaid is whitespace-sensitive).
    """
    lines = block.splitlines()
    if not lines or lines[0].strip() != "timeline":
        return False
    has_title = any(line.strip().startswith("title ") for line in lines[1:])
    has_section = any(line.strip().startswith("section ") for line in lines[1:])
    indented = all(
        (not line.strip()) or line.startswith(" ") or line.strip() == "timeline" for line in lines
    )
    return has_title and has_section and indented


class TestTimelineMermaid:
    async def test_mermaid_block_parses(self) -> None:
        app, ws_a, _ = _build_app()
        payload = await mcp_incident_timeline(
            app=app,
            alert_id=ws_a["alert"]["name"],
            workspace_id=uuid.UUID(ws_a["workspace_id"]),
        )
        block = render_mermaid(payload)
        assert _is_valid_mermaid_timeline(block), (
            f"render_mermaid output failed basic Mermaid timeline checks:\n{block}"
        )

    async def test_mermaid_escapes_colon_in_canonical_names(self) -> None:
        """Canonical names use ``://`` and ``:`` which break Mermaid section labels."""
        app, ws_a, _ = _build_app()
        payload = await mcp_incident_timeline(
            app=app,
            alert_id=ws_a["alert"]["name"],
            workspace_id=uuid.UUID(ws_a["workspace_id"]),
        )
        block = render_mermaid(payload)
        # The alert name uses 'alert://' which contains a colon. The
        # renderer must escape it; the raw form should never appear in
        # a Mermaid section label position.
        for line in block.splitlines():
            if line.strip().startswith("section "):
                assert ":" not in line.strip()[len("section ") :], (
                    f"section labels must not contain ':' — Mermaid breaks: {line!r}"
                )

    async def test_mermaid_empty_payload_still_parses(self) -> None:
        """A timeline with zero events renders an explanatory empty block."""
        app, ws_a, _ = _build_app()
        far_future = datetime(2099, 1, 1, tzinfo=UTC)
        payload = await mcp_incident_timeline(
            app=app,
            alert_id=ws_a["alert"]["name"],
            workspace_id=uuid.UUID(ws_a["workspace_id"]),
            from_ts=far_future,
        )
        block = render_mermaid(payload)
        assert _is_valid_mermaid_timeline(block)
        assert "No events" in block


# ---------------------------------------------------------------------------
# 5. Truncation safety
# ---------------------------------------------------------------------------


class TestTimelineTruncation:
    async def test_truncated_flag_is_false_for_small_fixture(self) -> None:
        app, ws_a, _ = _build_app()
        result = await mcp_incident_timeline(
            app=app,
            alert_id=ws_a["alert"]["name"],
            workspace_id=uuid.UUID(ws_a["workspace_id"]),
        )
        assert result["truncated"] is False
        assert len(result["events"]) <= MAX_EVENTS_RETURNED
