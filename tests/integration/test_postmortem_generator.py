"""Integration test for the post-mortem generator (issue #232 acceptance).

Wires the fixture incident through the full composition path:

* ``GraphStore`` plant -> ``mcp_incident_timeline(as_of=incident_end)``
* ``RunbookStepRecorder`` plant -> step events for the incident
* ``generate_postmortem()`` -> :class:`PostmortemReport`
* ``render()`` -> markdown / HTML / JSON

Acceptance from the issue:

* "fixture incident with 5+ entity state changes + 1 runbook step taken
  → assert generated markdown contains all 6 events as cited timeline
  rows, ordered by ts".

The fixture plants 6 entities (1 alert + 5 neighbours) all carrying a
``valid_from`` inside the incident window, plus 2 runbook-step events
(one ``executed``, one ``failed``).  That gives the test 6 timeline rows
and exercises both the cite-by-entity invariant and the FollowUp
extractor's "promote failed step" heuristic.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
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
from omniscience_server.postmortem import (
    SUPPORTED_TEMPLATES,
    PostmortemError,
    PostmortemTemplateId,
    generate_postmortem,
    render,
)
from omniscience_server.postmortem.errors import INCIDENT_NOT_FOUND_CODE
from omniscience_server.runbook import RunbookStepRecorder, record_runbook_step

# ---------------------------------------------------------------------------
# Fixture loader (mirrors tests/integration/test_incident_timeline.py)
# ---------------------------------------------------------------------------

_FIXTURE_PATH: Path = (
    Path(__file__).parent.parent / "fixtures" / "postmortem" / "workspace_incident.json"
)


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _node_from_dict(payload: dict[str, Any]) -> EntityNodeView:
    return EntityNodeView(
        id=uuid.uuid4(),
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


class _PostmortemGraphStore:
    """In-memory GraphStore fake — same shape as the timeline test's."""

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


def _build_app() -> tuple[FastAPI, dict[str, Any]]:
    payload = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    store = _PostmortemGraphStore()
    store.plant_workspace(payload)
    app = FastAPI()
    app.state.graph_store = store
    # Plant the runbook-step recorder + the fixture's step events so the
    # generator's FollowUp extractor sees them.
    recorder = RunbookStepRecorder()
    app.state.runbook_step_recorder = recorder
    workspace_id = uuid.UUID(payload["workspace_id"])
    for step in payload["runbook_steps"]:
        record_runbook_step(
            app=app,
            workspace_id=workspace_id,
            incident_id=payload["incident_id"],
            alert_id=step["alert_id"],
            runbook_name=step["runbook_name"],
            step_id=step["step_id"],
            outcome=step["outcome"],
            actor=step.get("actor"),
            note=step.get("note"),
            valid_from=_parse_dt(step.get("valid_from")),
        )
    return app, payload


# ---------------------------------------------------------------------------
# Acceptance: 6 cited events in the markdown, ordered by ts
# ---------------------------------------------------------------------------


class TestPostmortemAcceptance:
    async def test_generator_returns_six_cited_timeline_rows(self) -> None:
        app, fx = _build_app()
        report = await generate_postmortem(
            app=app,
            incident_id=fx["incident_id"],
            alert_id=fx["alert"]["name"],
            workspace_id=uuid.UUID(fx["workspace_id"]),
            template="blameless",
            incident_start=_parse_dt(fx["incident_start"]),
            incident_end=_parse_dt(fx["incident_end"]),
        )
        # Acceptance floor: alert + 5 neighbours = 6 created events.
        assert len(report.timeline) == 6
        # Every row carries a Citation with the event ts as its as_of.
        for row in report.timeline:
            assert row.citation.entity_id == row.entity_id
            assert row.citation.entity_type == row.entity_type
            assert row.citation.as_of == row.ts
            assert row.citation.source, "every row must carry source provenance"

    async def test_timeline_rows_are_ordered_by_ts(self) -> None:
        app, fx = _build_app()
        report = await generate_postmortem(
            app=app,
            incident_id=fx["incident_id"],
            alert_id=fx["alert"]["name"],
            workspace_id=uuid.UUID(fx["workspace_id"]),
            incident_start=_parse_dt(fx["incident_start"]),
            incident_end=_parse_dt(fx["incident_end"]),
        )
        for prev, curr in pairwise(report.timeline):
            assert prev.ts <= curr.ts, (
                f"timeline rows must be sorted ascending; "
                f"{prev.ts.isoformat()} > {curr.ts.isoformat()}"
            )

    async def test_markdown_contains_all_six_cited_rows(self) -> None:
        app, fx = _build_app()
        report = await generate_postmortem(
            app=app,
            incident_id=fx["incident_id"],
            alert_id=fx["alert"]["name"],
            workspace_id=uuid.UUID(fx["workspace_id"]),
            incident_start=_parse_dt(fx["incident_start"]),
            incident_end=_parse_dt(fx["incident_end"]),
        )
        markdown = render(report, fmt="markdown")
        # Each entity should appear in a cited row.
        for entity in [fx["alert"]] + fx["neighbours"]:
            assert entity["name"] in markdown, (
                f"entity {entity['name']} missing from rendered markdown"
            )
        # Header + every cited timeline ts must appear in the rendered
        # table (this is the issue's "all 6 events" acceptance bar).
        for row in report.timeline:
            assert row.ts.isoformat() in markdown
        # The cited-by-entity citation format is rendered as a backticked
        # 'type/id @ ts' fragment.
        first = report.timeline[0]
        assert (
            f"`{first.entity_type}/{first.entity_id}` @ {first.citation.as_of.isoformat()}"
            in markdown
        )

    async def test_followups_include_failed_runbook_step_and_baseline(self) -> None:
        app, fx = _build_app()
        report = await generate_postmortem(
            app=app,
            incident_id=fx["incident_id"],
            alert_id=fx["alert"]["name"],
            workspace_id=uuid.UUID(fx["workspace_id"]),
            incident_start=_parse_dt(fx["incident_start"]),
            incident_end=_parse_dt(fx["incident_end"]),
        )
        names = {f.name for f in report.followups}
        # baseline review
        assert any("schedule-review" in n for n in names)
        # failed-step FollowUp (the fixture has one `failed` step)
        assert any("step-step-2-restart-pool-failed" in n for n in names), (
            f"failed-step FollowUp missing; got: {sorted(names)}"
        )
        # Trigger phrase in the alert chunk_text promotes too
        assert any("alert-todo" in n for n in names), (
            f"TODO-phrase FollowUp missing; got: {sorted(names)}"
        )
        # canonical name shape
        for f in report.followups:
            assert f.name.startswith("followup://")
            assert f.incident_id == fx["incident_id"]
            assert f.alert_id == fx["alert"]["name"]

    @pytest.mark.parametrize("template", list(SUPPORTED_TEMPLATES))
    async def test_every_template_renders_with_citations(
        self, template: PostmortemTemplateId
    ) -> None:
        app, fx = _build_app()
        report = await generate_postmortem(
            app=app,
            incident_id=fx["incident_id"],
            alert_id=fx["alert"]["name"],
            workspace_id=uuid.UUID(fx["workspace_id"]),
            template=template,
            incident_start=_parse_dt(fx["incident_start"]),
            incident_end=_parse_dt(fx["incident_end"]),
        )
        markdown = render(report, fmt="markdown")
        assert report.template_id == template
        # Templates differ but the cited-timeline section must appear in
        # every rendered document.
        assert "| Timestamp (UTC) |" in markdown
        assert fx["alert"]["name"] in markdown

    async def test_html_render_smoke(self) -> None:
        app, fx = _build_app()
        report = await generate_postmortem(
            app=app,
            incident_id=fx["incident_id"],
            alert_id=fx["alert"]["name"],
            workspace_id=uuid.UUID(fx["workspace_id"]),
            template="coe",
            incident_start=_parse_dt(fx["incident_start"]),
            incident_end=_parse_dt(fx["incident_end"]),
        )
        html = render(report, fmt="html")
        assert html.startswith("<!doctype html>")
        assert "<table" in html
        assert fx["incident_id"] in html

    async def test_unknown_alert_raises_incident_not_found(self) -> None:
        app, fx = _build_app()
        with pytest.raises(PostmortemError) as excinfo:
            await generate_postmortem(
                app=app,
                incident_id=fx["incident_id"],
                alert_id="alert://pagerduty/does-not-exist",
                workspace_id=uuid.UUID(fx["workspace_id"]),
            )
        assert excinfo.value.code == INCIDENT_NOT_FOUND_CODE

    async def test_runbook_steps_count_matches_recorder(self) -> None:
        app, fx = _build_app()
        report = await generate_postmortem(
            app=app,
            incident_id=fx["incident_id"],
            alert_id=fx["alert"]["name"],
            workspace_id=uuid.UUID(fx["workspace_id"]),
            incident_start=_parse_dt(fx["incident_start"]),
            incident_end=_parse_dt(fx["incident_end"]),
        )
        assert report.runbook_steps_count == len(fx["runbook_steps"])

    async def test_cross_workspace_alert_is_not_found(self) -> None:
        """Workspace ACL invariant — alerts are invisible across workspaces."""
        app, fx = _build_app()
        other_workspace = uuid.UUID("ffffffff-0000-0000-0000-000000000000")
        with pytest.raises(PostmortemError) as excinfo:
            await generate_postmortem(
                app=app,
                incident_id=fx["incident_id"],
                alert_id=fx["alert"]["name"],
                workspace_id=other_workspace,
            )
        assert excinfo.value.code == INCIDENT_NOT_FOUND_CODE

    async def test_generated_at_is_now_when_overridden(self) -> None:
        app, fx = _build_app()
        frozen = datetime(2026, 4, 13, 0, 0, tzinfo=UTC)
        report = await generate_postmortem(
            app=app,
            incident_id=fx["incident_id"],
            alert_id=fx["alert"]["name"],
            workspace_id=uuid.UUID(fx["workspace_id"]),
            now=frozen,
        )
        assert report.generated_at == frozen
        # Without explicit incident_end, the generator uses ``now`` as
        # the bitemporal anchor.
        assert report.incident_end == frozen
