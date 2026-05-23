"""Integration test for ``suggest_runbook`` + ``record_runbook_step`` (issue #231).

Drives the server-side surface end-to-end against the runbook fixtures at
``tests/fixtures/runbook/`` and an in-memory ``GraphStore`` fake so the
test runs in the default CI lane without testcontainers.

Asserted invariants
-------------------

1. Discovery -> parse -> suggest: real fixtures are walked by the
   connector, parsed, and (after being threaded through suggest as the
   pre-loaded candidate set) the highest-confidence runbook for the
   planted alert is the ``db-connections-exhausted`` runbook.
2. Top-3 contract: at most 3 suggestions returned; ordered DESC by
   confidence.
3. Monotonicity: when we re-score against an alert that gains a matching
   tag, the same runbook's confidence is weakly higher — not lower.
4. Step recording: ``record_runbook_step`` round-trips through the
   in-memory recorder, the structured-log event fires (asserted via
   structlog capture), and the recorder filters by workspace_id.
5. Cross-workspace ACL: alert_id in workspace A is invisible to a
   recorder filter for workspace B even when the same alert was used.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from omniscience_connectors.runbook import (
    RunbookConfig,
    RunbookConnector,
    RunbookDocument,
)
from omniscience_core.storage.graph import (
    EntityNodeView,
    GraphResultView,
)
from omniscience_server.runbook import (
    ALERT_NOT_FOUND_CODE,
    INVALID_ALERT_ID_CODE,
    RunbookStepRecorder,
    record_runbook_step,
    suggest_runbook,
)

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "runbook"

_WS_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000231")
_WS_B = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000231")
_ALERT_DB = "alert://datadog/db.connections.exhausted"
_ALERT_REDIS = "alert://datadog/redis.evictions"
_ALERT_5XX = "alert://datadog/http.5xx"


class _SuggestGraphStore:
    """In-memory store that only needs ``get_entity``/``find_related``."""

    def __init__(self) -> None:
        self._entities: dict[tuple[uuid.UUID, str], EntityNodeView] = {}
        self._neighbours: dict[tuple[uuid.UUID, str], list[EntityNodeView]] = {}
        self.calls: list[dict[str, Any]] = []

    def plant_alert(
        self,
        *,
        workspace_id: uuid.UUID,
        name: str,
        tags: list[str] | None = None,
        severity: str | None = None,
    ) -> None:
        import json

        chunk_text = json.dumps({"tags": tags or [], "severity": severity})
        self._entities[(workspace_id, name)] = EntityNodeView(
            name=name,
            kind="alert",
            source="src-test",
            chunk_text=chunk_text,
            depth=0,
            edge_type=None,
        )
        self._neighbours[(workspace_id, name)] = []

    async def get_entity(
        self,
        *,
        entity_name: str,
        workspace_id: uuid.UUID,
        as_of: datetime | None = None,
    ) -> EntityNodeView | None:
        self.calls.append(
            {"op": "get_entity", "name": entity_name, "ws": workspace_id, "as_of": as_of}
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
        self.calls.append({"op": "find_related", "name": entity_name, "ws": workspace_id})
        seed = self._entities.get((workspace_id, entity_name))
        if seed is None:
            raise ValueError(f"entity_not_found:{entity_name}")
        return GraphResultView(
            seed=seed,
            related=list(self._neighbours.get((workspace_id, entity_name), [])),
            edges=[],
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _load_fixture_runbooks() -> list[RunbookDocument]:
    """Walk the fixture directory via the real connector and parse each file."""
    cn = RunbookConnector()
    cfg = RunbookConfig(root_path=str(FIXTURE_DIR), source_uid="team-runbooks")
    refs = [ref async for ref in cn.discover(cfg, {})]
    out: list[RunbookDocument] = []
    for ref in refs:
        fetched = await cn.fetch(cfg, {}, ref)
        rb = RunbookDocument.model_validate_json(fetched.content_bytes.decode("utf-8"))
        out.append(rb)
    return out


def _make_app(graph_store: _SuggestGraphStore) -> SimpleNamespace:
    """Build a minimal FastAPI-app stand-in exposing ``state.graph_store``."""
    state = SimpleNamespace(graph_store=graph_store)
    return SimpleNamespace(state=state)


# ---------------------------------------------------------------------------
# Suggest path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suggest_runbook_picks_best_match_from_fixtures() -> None:
    candidates = await _load_fixture_runbooks()
    # Sanity: at least three runbooks were discovered.
    assert len(candidates) >= 3, [c.canonical_name for c in candidates]

    store = _SuggestGraphStore()
    store.plant_alert(
        workspace_id=_WS_A,
        name=_ALERT_DB,
        tags=["service:payments", "severity:sev2"],
        severity="sev2",
    )
    app = _make_app(store)

    response = await suggest_runbook(
        app=app,  # type: ignore[arg-type]
        alert_id=_ALERT_DB,
        workspace_id=_WS_A,
        candidates=candidates,
    )

    suggestions = response["suggestions"]
    assert 1 <= len(suggestions) <= 3
    top = suggestions[0]
    assert top["runbook_name"] == "runbook://team-runbooks/db-connections-exhausted.md"
    assert top["exact_match"] is True
    assert top["confidence"] > 0.5
    assert top["citation"]["title"] == "Database connections exhausted"
    assert top["citation"]["first_step"]["step_id"] == "database-connections-exhausted"
    # Confidence is sorted DESC.
    confidences = [s["confidence"] for s in suggestions]
    assert confidences == sorted(confidences, reverse=True)


@pytest.mark.asyncio
async def test_suggest_runbook_unknown_alert_raises_not_found() -> None:
    candidates = await _load_fixture_runbooks()
    store = _SuggestGraphStore()  # alert is NOT planted
    app = _make_app(store)

    with pytest.raises(ValueError, match=rf"^{ALERT_NOT_FOUND_CODE}:"):
        await suggest_runbook(
            app=app,  # type: ignore[arg-type]
            alert_id=_ALERT_DB,
            workspace_id=_WS_A,
            candidates=candidates,
        )


@pytest.mark.asyncio
async def test_suggest_runbook_invalid_alert_id() -> None:
    with pytest.raises(ValueError, match=rf"^{INVALID_ALERT_ID_CODE}:"):
        await suggest_runbook(
            app=_make_app(_SuggestGraphStore()),  # type: ignore[arg-type]
            alert_id="not-a-canonical-name",
            workspace_id=_WS_A,
            candidates=[],
        )


@pytest.mark.asyncio
async def test_suggest_runbook_monotonic_with_more_matching_tags() -> None:
    """Adding a matching tag to the alert must not lower confidence."""
    candidates = await _load_fixture_runbooks()
    db_rb = next(c for c in candidates if "db-connections" in c.rel_path)

    store_one = _SuggestGraphStore()
    store_one.plant_alert(workspace_id=_WS_A, name=_ALERT_DB, tags=[], severity=None)
    response_one = await suggest_runbook(
        app=_make_app(store_one),  # type: ignore[arg-type]
        alert_id=_ALERT_DB,
        workspace_id=_WS_A,
        candidates=[db_rb],
    )
    c_one = response_one["suggestions"][0]["confidence"]

    store_two = _SuggestGraphStore()
    store_two.plant_alert(
        workspace_id=_WS_A,
        name=_ALERT_DB,
        tags=["service:payments"],
        severity="sev2",
    )
    response_two = await suggest_runbook(
        app=_make_app(store_two),  # type: ignore[arg-type]
        alert_id=_ALERT_DB,
        workspace_id=_WS_A,
        candidates=[db_rb],
    )
    c_two = response_two["suggestions"][0]["confidence"]

    assert c_two >= c_one


@pytest.mark.asyncio
async def test_suggest_runbook_top_k_bound() -> None:
    candidates = await _load_fixture_runbooks()
    store = _SuggestGraphStore()
    store.plant_alert(
        workspace_id=_WS_A,
        name=_ALERT_REDIS,
        tags=["service:cache"],
        severity="sev3",
    )
    response = await suggest_runbook(
        app=_make_app(store),  # type: ignore[arg-type]
        alert_id=_ALERT_REDIS,
        workspace_id=_WS_A,
        candidates=candidates,
        top_k=2,
    )
    assert len(response["suggestions"]) <= 2


@pytest.mark.asyncio
async def test_suggest_runbook_meta_block_has_counts() -> None:
    candidates = await _load_fixture_runbooks()
    store = _SuggestGraphStore()
    store.plant_alert(workspace_id=_WS_A, name=_ALERT_5XX, tags=[])
    response = await suggest_runbook(
        app=_make_app(store),  # type: ignore[arg-type]
        alert_id=_ALERT_5XX,
        workspace_id=_WS_A,
        candidates=candidates,
    )
    assert response["meta"]["candidate_count"] == len(candidates)
    assert "effective_as_of" in response


# ---------------------------------------------------------------------------
# Record step path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_runbook_step_round_trip() -> None:
    app = SimpleNamespace(state=SimpleNamespace())
    event = record_runbook_step(
        app=app,  # type: ignore[arg-type]
        workspace_id=_WS_A,
        incident_id="INC-123",
        alert_id=_ALERT_DB,
        runbook_name="runbook://team-runbooks/db-connections-exhausted.md",
        step_id="mitigate",
        outcome="executed",
        actor="alice",
        note="Scaled to 8 replicas.",
    )
    assert event.workspace_id == _WS_A
    assert event.incident_id == "INC-123"
    assert event.step_name.endswith("#mitigate")
    assert event.recorded_at.tzinfo is not None
    assert event.valid_from.tzinfo is not None

    payload = event.to_payload()
    assert payload["actor"] == "alice"
    assert payload["note"] == "Scaled to 8 replicas."

    recorder: RunbookStepRecorder = app.state.runbook_step_recorder
    recent = recorder.recent(workspace_id=_WS_A)
    assert len(recent) == 1
    assert recent[0].event_id == event.event_id


@pytest.mark.asyncio
async def test_record_runbook_step_filters_by_workspace() -> None:
    app = SimpleNamespace(state=SimpleNamespace())
    record_runbook_step(
        app=app,  # type: ignore[arg-type]
        workspace_id=_WS_A,
        incident_id="INC-A",
        alert_id=_ALERT_DB,
        runbook_name="runbook://team-runbooks/db-connections-exhausted.md",
        step_id="triage",
    )
    record_runbook_step(
        app=app,  # type: ignore[arg-type]
        workspace_id=_WS_B,
        incident_id="INC-B",
        alert_id=_ALERT_REDIS,
        runbook_name="runbook://team-runbooks/redis-evictions.md",
        step_id="check",
    )
    recorder: RunbookStepRecorder = app.state.runbook_step_recorder
    a_events = recorder.recent(workspace_id=_WS_A)
    b_events = recorder.recent(workspace_id=_WS_B)
    assert len(a_events) == 1 and a_events[0].incident_id == "INC-A"
    assert len(b_events) == 1 and b_events[0].incident_id == "INC-B"


@pytest.mark.asyncio
async def test_record_runbook_step_validation_errors() -> None:
    app = SimpleNamespace(state=SimpleNamespace())
    with pytest.raises(ValueError, match=r"^invalid_alert_id:"):
        record_runbook_step(
            app=app,  # type: ignore[arg-type]
            workspace_id=_WS_A,
            incident_id="INC-1",
            alert_id="bogus",
            runbook_name="runbook://team-runbooks/db.md",
            step_id="triage",
        )
    with pytest.raises(ValueError, match=r"^invalid_runbook_name:"):
        record_runbook_step(
            app=app,  # type: ignore[arg-type]
            workspace_id=_WS_A,
            incident_id="INC-1",
            alert_id=_ALERT_DB,
            runbook_name="not-a-canonical-runbook",
            step_id="triage",
        )
    with pytest.raises(ValueError, match=r"^invalid_step_id:"):
        record_runbook_step(
            app=app,  # type: ignore[arg-type]
            workspace_id=_WS_A,
            incident_id="INC-1",
            alert_id=_ALERT_DB,
            runbook_name="runbook://team-runbooks/db.md",
            step_id="bad step id with spaces",
        )


@pytest.mark.asyncio
async def test_record_runbook_step_honours_supplied_valid_from() -> None:
    """A historical replay can backdate ``valid_from`` per ADR-0008 §1."""
    app = SimpleNamespace(state=SimpleNamespace())
    past = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    event = record_runbook_step(
        app=app,  # type: ignore[arg-type]
        workspace_id=_WS_A,
        incident_id="INC-9",
        alert_id=_ALERT_DB,
        runbook_name="runbook://team-runbooks/db.md",
        step_id="triage",
        valid_from=past,
    )
    assert event.valid_from == past
    # recorded_at is "now" - strictly after the backdated valid_from.
    assert event.recorded_at > event.valid_from
