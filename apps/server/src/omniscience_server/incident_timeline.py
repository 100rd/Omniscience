"""Incident timeline composition (issue #235, H5 Track 1 of epic #230).

Given an ``alert_id``, this module walks the alert's blast-radius
neighbourhood via ``GraphStore.find_related`` (workspace-scoped,
``as_of``-aware) and projects each entity's bitemporal triple into a
chronologically ordered list of change events.  The result is the
"5-minute wow" demo for SRE pilots: a story-ready timeline rendered as
either JSON or a Mermaid block embeddable in markdown.

Composition shape
-----------------

1. **Parse** ``alert_id``: reuses :func:`~omniscience_server.incidents._validate_alert_id`
   so the contract is byte-identical to ``resolve_incident``
   (``invalid_alert_id`` on bad shapes).
2. **Resolve** the seed alert via :meth:`GraphStore.get_entity`.
   ``404 alert_not_found`` on absence — cross-workspace alerts are
   indistinguishable from "not found" by design (#117).
3. **Traverse** outward via :meth:`GraphStore.find_related` (depth
   :data:`TIMELINE_MAX_DEPTH`, no edge filter — we want every neighbour
   in the blast radius represented).
4. **Project** each ``EntityNodeView`` into one or two events:
   - ``valid_from`` -> ``"created"`` event.
   - ``valid_to`` -> ``"ended"`` event (only when populated).
5. **Filter** by ``[from, to)`` event-timestamp window and optional
   ``entity_types`` allowlist.
6. **Order** the resulting events by ``ts`` ascending; ties broken by
   ``entity_id`` for deterministic output.

ACL invariant
-------------

``workspace_id`` is taken from the caller's bearer token; never trusted
from input.  Every store call carries it.  Cross-workspace data is
unreachable per the protocol's ACL contract (ADR-0005 / #117).

Determinism
-----------

The Mermaid renderer escapes the small set of glyphs that break
``timeline`` syntax (``:``, newline, pipe, quote) so any canonical name
in the codebase (``alert://`` / ``arn:`` / ``slack://channel/...``) is
safe to embed.  The renderer is pure; the basic parse-check used by the
integration test is local to the test module.

Bitemporal contract
-------------------

ADR-0008 §1 — entity rows carry ``valid_from``, ``valid_to``, and
``recorded_at``.  Legacy pre-#130 rows leave them ``None`` and are
filtered out of the timeline (we cannot place them on a clock).  Events
expose both ``ts`` (the user-visible change time, sourced from
``valid_from``/``valid_to``) and ``source`` (the connector that wrote
the row) so reviewers can audit provenance without a second lookup.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

from fastapi import FastAPI
from omniscience_core.storage import EntityNodeView, GraphResultView, GraphStore
from pydantic import BaseModel, Field

from omniscience_server.as_of import (
    enforce_utc,
    record_request_duration,
    resolve_effective_as_of,
)
from omniscience_server.incidents import (
    ALERT_NOT_FOUND_CODE,
    _validate_alert_id,  # re-used: same alert_id grammar as resolve_incident
)

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

#: Default BFS depth when composing a timeline.  Matches
#: :data:`~omniscience_server.incidents.DEFAULT_MAX_DEPTH` so the timeline
#: window sees the same blast radius that ``resolve_incident`` does.
TIMELINE_MAX_DEPTH: Final[int] = 2

#: Hard cap on events returned in a single response.  Mirrors the
#: pagination floor used by other admin endpoints — incidents with more
#: than this many state changes need a paged view (#236 follow-on).
MAX_EVENTS_RETURNED: Final[int] = 500

#: Change kinds emitted by the timeline projection.  ``Literal`` so the
#: REST/MCP wire schema is closed and mypy-checked.
ChangeKind = Literal["created", "ended"]

#: Output formats supported by the REST endpoint.
TimelineFormat = Literal["json", "mermaid"]

# ---------------------------------------------------------------------------
# Pydantic response schemas (wire format)
# ---------------------------------------------------------------------------


class TimelineEvent(BaseModel):
    """A single bitemporal change projected from an entity row."""

    ts: datetime = Field(description="Event timestamp (UTC, ISO-8601).")
    entity_id: str = Field(description="Canonical entity name.")
    entity_type: str = Field(description="Entity kind (e.g. 'k8s_pod', 'pull_request').")
    change_kind: ChangeKind = Field(description="'created' or 'ended'.")
    before_state_summary: str | None = Field(
        default=None,
        description=(
            "Human-readable description of the prior state. Null for "
            "'created' events (no prior state)."
        ),
    )
    after_state_summary: str | None = Field(
        default=None,
        description=(
            "Human-readable description of the resulting state. Null "
            "for 'ended' events (the entity no longer has a valid state)."
        ),
    )
    source: str = Field(
        description=(
            "Connector / source identifier that wrote the underlying "
            "row (provenance — see ADR-0008 §1 recorded_at companion)."
        ),
    )


class IncidentTimelineResponse(BaseModel):
    """The full timeline payload — events plus envelope metadata."""

    alert_id: str
    events: list[TimelineEvent] = Field(default_factory=list)
    effective_as_of: datetime
    window_from: datetime | None = None
    window_to: datetime | None = None
    entity_types_filter: list[str] | None = None
    truncated: bool = Field(
        default=False,
        description="True iff the result was clipped at MAX_EVENTS_RETURNED.",
    )


# ---------------------------------------------------------------------------
# Internal projection dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ProjectedEvent:
    """Pre-validation view of a single event candidate."""

    ts: datetime
    entity_id: str
    entity_type: str
    change_kind: ChangeKind
    before_state_summary: str | None
    after_state_summary: str | None
    source: str


# ---------------------------------------------------------------------------
# Public composition entry point
# ---------------------------------------------------------------------------


async def mcp_incident_timeline(
    app: FastAPI,
    *,
    alert_id: str,
    workspace_id: uuid.UUID,
    as_of: datetime | None = None,
    from_ts: datetime | None = None,
    to_ts: datetime | None = None,
    entity_types: list[str] | None = None,
    max_depth: int = TIMELINE_MAX_DEPTH,
) -> dict[str, Any]:
    """Compose the timeline payload for ``alert_id`` within ``workspace_id``.

    Returns a JSON-serialisable dict matching :class:`IncidentTimelineResponse`.

    Parameters
    ----------
    app:
        FastAPI app exposing ``app.state.graph_store``.
    alert_id:
        Canonical alert URI ``alert://{provider}/{provider_alert_id}``.
    workspace_id:
        REQUIRED. Forwarded to every store call (ACL invariant).
    as_of:
        Optional bitemporal anchor.  Forwarded to ``GraphStore``;
        timeline events are projected from the row state visible at T.
    from_ts, to_ts:
        Half-open event-time window ``[from_ts, to_ts)`` applied to
        the projected events.  ``None`` on either side leaves that
        side unbounded.  Both bounds must be timezone-aware UTC per
        the as_of contract.
    entity_types:
        Optional allowlist of entity ``kind`` strings.  Empty list /
        ``None`` means "no filter".
    max_depth:
        BFS depth from the alert seed.  Defaults to
        :data:`TIMELINE_MAX_DEPTH`.
    """
    _validate_alert_id(alert_id)
    normalised_as_of = enforce_utc(as_of)
    normalised_from = enforce_utc(from_ts)
    normalised_to = enforce_utc(to_ts)
    clamped_depth = max(1, min(max_depth, 5))
    type_filter: set[str] | None = {t for t in entity_types if t} if entity_types else None

    graph_store: GraphStore | None = getattr(app.state, "graph_store", None)
    if graph_store is None:
        raise RuntimeError("graph_store not available on app.state")

    with record_request_duration(
        surface="mcp", tool="incident_timeline", as_of=normalised_as_of
    ) as patcher:
        seed = await graph_store.get_entity(
            entity_name=alert_id,
            workspace_id=workspace_id,
            as_of=normalised_as_of,
        )
        if seed is None:
            if normalised_as_of is not None:
                patcher.mark_pre_history()
            raise ValueError(f"{ALERT_NOT_FOUND_CODE}:{alert_id}")

        try:
            graph_result = await graph_store.find_related(
                entity_name=alert_id,
                workspace_id=workspace_id,
                as_of=normalised_as_of,
                max_depth=clamped_depth,
            )
        except ValueError as exc:
            if str(exc).startswith("entity_not_found:"):
                # Race against re-ingestion — keep the timeline alert-only.
                graph_result = GraphResultView(seed=seed, related=[], edges=[])
            else:
                raise

        projected = _project_events(seed=seed, result=graph_result)
        filtered = _apply_filters(
            projected,
            from_ts=normalised_from,
            to_ts=normalised_to,
            entity_types=type_filter,
        )
        ordered = sorted(filtered, key=lambda e: (e.ts, e.entity_id, e.change_kind))

        truncated = len(ordered) > MAX_EVENTS_RETURNED
        if truncated:
            ordered = ordered[:MAX_EVENTS_RETURNED]

        response = IncidentTimelineResponse(
            alert_id=alert_id,
            events=[_to_wire(e) for e in ordered],
            effective_as_of=resolve_effective_as_of(normalised_as_of),
            window_from=normalised_from,
            window_to=normalised_to,
            entity_types_filter=(sorted(type_filter) if type_filter else None),
            truncated=truncated,
        )
        return response.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Projection helpers — pure, unit-testable
# ---------------------------------------------------------------------------


def _project_events(
    *,
    seed: EntityNodeView,
    result: GraphResultView,
) -> list[_ProjectedEvent]:
    """Project the seed + every neighbour into ``_ProjectedEvent`` candidates.

    One ``created`` event per row with a populated ``valid_from``; one
    ``ended`` event per row that also carries ``valid_to`` (i.e. the
    state has been tombstoned or superseded — ADR-0008 §5).
    """
    candidates: list[EntityNodeView] = [seed, *result.related]
    out: list[_ProjectedEvent] = []
    for node in candidates:
        out.extend(_events_for_node(node))
    return out


def _events_for_node(node: EntityNodeView) -> list[_ProjectedEvent]:
    """Emit zero, one, or two events for a single entity row."""
    events: list[_ProjectedEvent] = []
    summary = _summarise(node)
    if node.valid_from is not None:
        events.append(
            _ProjectedEvent(
                ts=_to_utc(node.valid_from),
                entity_id=node.name,
                entity_type=node.kind,
                change_kind="created",
                before_state_summary=None,
                after_state_summary=summary,
                source=node.source,
            )
        )
    if node.valid_to is not None:
        events.append(
            _ProjectedEvent(
                ts=_to_utc(node.valid_to),
                entity_id=node.name,
                entity_type=node.kind,
                change_kind="ended",
                before_state_summary=summary,
                after_state_summary=None,
                source=node.source,
            )
        )
    return events


def _summarise(node: EntityNodeView) -> str:
    """Build a compact human-readable summary for the event row.

    Uses the chunk text when present (it's the connector's most
    detailed prose) and falls back to ``"{kind} {name}"``.  Truncated to
    keep wire payloads small.
    """
    if node.chunk_text:
        text = node.chunk_text.strip()
        if len(text) > 200:
            text = text[:197] + "..."
        return text
    return f"{node.kind} {node.name}"


def _to_utc(ts: datetime) -> datetime:
    """Coerce to UTC; fixtures sometimes carry naive times — be defensive."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def _apply_filters(
    events: list[_ProjectedEvent],
    *,
    from_ts: datetime | None,
    to_ts: datetime | None,
    entity_types: set[str] | None,
) -> list[_ProjectedEvent]:
    """Apply the ``[from, to)`` window and ``entity_types`` allowlist."""
    out: list[_ProjectedEvent] = []
    for event in events:
        if from_ts is not None and event.ts < from_ts:
            continue
        if to_ts is not None and event.ts >= to_ts:
            continue
        if entity_types is not None and event.entity_type not in entity_types:
            continue
        out.append(event)
    return out


def _to_wire(event: _ProjectedEvent) -> TimelineEvent:
    """Convert the internal dataclass to the public Pydantic model."""
    return TimelineEvent(
        ts=event.ts,
        entity_id=event.entity_id,
        entity_type=event.entity_type,
        change_kind=event.change_kind,
        before_state_summary=event.before_state_summary,
        after_state_summary=event.after_state_summary,
        source=event.source,
    )


# ---------------------------------------------------------------------------
# Mermaid renderer
# ---------------------------------------------------------------------------

#: Mermaid timeline header.  ``title`` is required so renderers display
#: the alert id at the top of the diagram.
_MERMAID_HEADER: Final[str] = "timeline"

#: Glyphs that break Mermaid ``timeline`` syntax when embedded in a
#: section/event label.  ``:`` is the section terminator; newlines and
#: pipes are control characters; quotes confuse the lexer.
_MERMAID_ESCAPES: Final[dict[str, str]] = {
    ":": "-",  # Mermaid section/event terminator; replace with hyphen
    "\n": " ",
    "\r": " ",
    "|": "/",
    '"': "'",
}


def render_mermaid(payload: dict[str, Any]) -> str:
    """Render an :class:`IncidentTimelineResponse` payload as Mermaid.

    The output is a single ``timeline`` block ready to embed in any
    markdown renderer that supports Mermaid (GitHub, GitLab, Notion,
    most static-site generators).  Days are used as the section
    granularity so multi-day incidents stay readable.

    The function is pure and side-effect-free; it accepts the already
    serialised wire dict so callers can render the same payload they'd
    return as JSON.
    """
    lines: list[str] = [_MERMAID_HEADER]
    title = _escape(f"Incident {payload.get('alert_id', '<unknown>')}")
    lines.append(f"    title {title}")

    events = payload.get("events") or []
    if not events:
        # Mermaid renderers accept an empty timeline but mostly render
        # a blank box — surface a single explanatory section.
        lines.append("    section No events in window")
        lines.append("        (empty) : Try widening from/to or entity_types")
        return "\n".join(lines)

    current_section: str | None = None
    for event in events:
        ts_iso: str = str(event.get("ts", ""))
        section = ts_iso[:10] if ts_iso else "unknown"
        if section != current_section:
            lines.append(f"    section {_escape(section)}")
            current_section = section

        time_part = ts_iso[11:19] if len(ts_iso) >= 19 else ts_iso
        entity = _escape(str(event.get("entity_id", "")))
        kind = _escape(str(event.get("entity_type", "")))
        change = _escape(str(event.get("change_kind", "")))
        summary = event.get("after_state_summary") or event.get("before_state_summary") or ""
        summary_short = _escape(str(summary))[:120]
        label = f"{time_part} {kind} {change}"
        body = f"{entity} - {summary_short}" if summary_short else entity
        lines.append(f"        {label} : {body}")

    return "\n".join(lines)


def _escape(value: str) -> str:
    """Replace Mermaid-breaking glyphs with safe equivalents."""
    out = value
    for needle, replacement in _MERMAID_ESCAPES.items():
        out = out.replace(needle, replacement)
    return out


__all__ = [
    "MAX_EVENTS_RETURNED",
    "TIMELINE_MAX_DEPTH",
    "ChangeKind",
    "IncidentTimelineResponse",
    "TimelineEvent",
    "TimelineFormat",
    "mcp_incident_timeline",
    "render_mermaid",
]
