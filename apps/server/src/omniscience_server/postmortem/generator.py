"""Core post-mortem composition + rendering (issue #232).

Composition flow
----------------

1. Resolve template via :func:`get_template`.
2. Read the bitemporal timeline for ``alert_id`` between
   ``incident_start`` and ``incident_end`` via
   :func:`mcp_incident_timeline` with ``as_of=incident_end``.
3. Pull recent runbook-step events for ``incident_id`` from the
   :class:`RunbookStepRecorder` ring buffer.
4. Build a :class:`PostmortemReport` with cited timeline rows + extracted
   follow-ups.
5. (Optional) render the report to markdown or HTML.

The composition is workspace-scoped: every store call carries
``workspace_id``; the runbook-step recorder also filters by workspace.
"""

from __future__ import annotations

import html
import uuid
from datetime import UTC, datetime
from typing import Any, Final, Literal

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field

from omniscience_server.as_of import enforce_utc, resolve_effective_as_of
from omniscience_server.incident_timeline import mcp_incident_timeline
from omniscience_server.incidents import (
    ALERT_NOT_FOUND_CODE,
    INVALID_ALERT_ID_CODE,
)
from omniscience_server.postmortem.errors import (
    INCIDENT_NOT_FOUND_CODE,
    INVALID_FORMAT_CODE,
    INVALID_INCIDENT_ID_CODE,
    PostmortemError,
)
from omniscience_server.postmortem.followups import FollowUp, extract_followups
from omniscience_server.postmortem.models import Citation, PostmortemTimelineRow
from omniscience_server.postmortem.templates import (
    PostmortemTemplate,
    PostmortemTemplateId,
    TemplateSection,
    get_template,
)

#: Output formats the generator can render.  ``json`` returns the raw
#: structured report (handy for API clients that want to template
#: themselves); ``markdown`` is the canonical human-readable form.
PostmortemFormat = Literal["markdown", "html", "json"]

#: Concrete tuple matching :data:`PostmortemFormat` for runtime checks.
SUPPORTED_FORMATS: Final[tuple[PostmortemFormat, ...]] = (
    "markdown",
    "html",
    "json",
)

#: Maximum incident_id length the surface accepts.  Matches the REST
#: ``incident_id`` path parameter on the runbook router so the two
#: endpoints share an identifier shape.
_MAX_INCIDENT_ID_LEN: Final[int] = 256


# ---------------------------------------------------------------------------
# Structured report
# ---------------------------------------------------------------------------


class PostmortemReport(BaseModel):
    """The full structured post-mortem.

    Returned by :func:`generate_postmortem`; consumed by :func:`render`
    or serialised straight to JSON for callers that want to format the
    document themselves.
    """

    model_config = ConfigDict(arbitrary_types_allowed=False)

    incident_id: str
    alert_id: str
    template_id: PostmortemTemplateId
    template_display_name: str
    incident_start: datetime
    incident_end: datetime
    generated_at: datetime
    effective_as_of: datetime = Field(
        description=(
            "The bitemporal anchor used to read the timeline — usually "
            "``incident_end`` so the graph snapshot reflects the state "
            "at incident close."
        ),
    )
    timeline: list[PostmortemTimelineRow] = Field(default_factory=list)
    runbook_steps_count: int = Field(
        default=0,
        description="Count of recorded runbook-step events for the incident.",
    )
    followups: list[FollowUp] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_incident_id(incident_id: str) -> None:
    if not isinstance(incident_id, str) or not incident_id:
        raise PostmortemError(
            INVALID_INCIDENT_ID_CODE,
            "incident_id must be a non-empty string",
        )
    if len(incident_id) > _MAX_INCIDENT_ID_LEN:
        raise PostmortemError(
            INVALID_INCIDENT_ID_CODE,
            f"incident_id must be <= {_MAX_INCIDENT_ID_LEN} characters",
        )


def _validate_format(fmt: str) -> PostmortemFormat:
    if fmt not in SUPPORTED_FORMATS:
        raise PostmortemError(
            INVALID_FORMAT_CODE,
            f"format must be one of: {', '.join(SUPPORTED_FORMATS)}",
        )
    return fmt


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


async def generate_postmortem(
    *,
    app: FastAPI,
    incident_id: str,
    alert_id: str,
    workspace_id: uuid.UUID,
    template: str = "blameless",
    incident_start: datetime | None = None,
    incident_end: datetime | None = None,
    now: datetime | None = None,
) -> PostmortemReport:
    """Compose a structured :class:`PostmortemReport`.

    Parameters
    ----------
    app:
        FastAPI app exposing ``app.state.graph_store`` and (optionally)
        ``app.state.runbook_step_recorder``.
    incident_id:
        Caller-supplied incident identifier (PagerDuty incident id, etc.)
        — used to scope the runbook-step lookup and stamp FollowUps.
    alert_id:
        Canonical alert URI ``alert://{provider}/{provider_alert_id}``.
    workspace_id:
        REQUIRED. Forwarded to every store call (ACL invariant).
    template:
        Template id; see :data:`SUPPORTED_TEMPLATES`.  Defaults to
        ``blameless``.
    incident_start, incident_end:
        Half-open ``[start, end)`` window applied to the timeline; both
        must be timezone-aware UTC.  When ``incident_start`` is ``None``
        the timeline is unbounded on the left; ``incident_end`` defaults
        to ``now``.
    now:
        Override for deterministic tests; defaults to ``datetime.now(UTC)``.
    """
    _validate_incident_id(incident_id)
    selected_template = get_template(template)

    normalised_now = (now or datetime.now(UTC)).astimezone(UTC)
    normalised_start = enforce_utc(incident_start)
    normalised_end = enforce_utc(incident_end) or normalised_now

    if normalised_start is not None and normalised_start > normalised_end:
        raise PostmortemError(
            INVALID_INCIDENT_ID_CODE,
            "incident_start must be <= incident_end",
        )

    # 1. Pull the timeline at ``as_of=incident_end`` so the graph snapshot
    # reflects the bitemporal state at incident close (issue #232 spec).
    try:
        raw_timeline = await mcp_incident_timeline(
            app=app,
            alert_id=alert_id,
            workspace_id=workspace_id,
            as_of=normalised_end,
            from_ts=normalised_start,
            to_ts=normalised_end,
        )
    except ValueError as exc:
        msg = str(exc)
        if msg.startswith(f"{ALERT_NOT_FOUND_CODE}:"):
            raise PostmortemError(
                INCIDENT_NOT_FOUND_CODE,
                f"alert '{alert_id}' not found for incident '{incident_id}'",
            ) from exc
        if msg.startswith(f"{INVALID_ALERT_ID_CODE}:"):
            # Surface upstream validator's code unchanged.
            raise
        raise

    timeline_rows = _project_timeline_rows(raw_timeline, default_as_of=normalised_end)

    # 2. Look up runbook-step events for this incident from the recorder
    # (process-local; future PR promotes to a SQL table — see #231).
    runbook_steps = _load_runbook_steps(app, workspace_id=workspace_id, incident_id=incident_id)

    # 3. Extract follow-ups.
    followups = extract_followups(
        incident_id=incident_id,
        alert_id=alert_id,
        timeline=timeline_rows,
        runbook_steps=runbook_steps,
        now=normalised_now,
    )

    return PostmortemReport(
        incident_id=incident_id,
        alert_id=alert_id,
        template_id=selected_template.template_id,
        template_display_name=selected_template.display_name,
        incident_start=normalised_start or _earliest_ts(timeline_rows, fallback=normalised_end),
        incident_end=normalised_end,
        generated_at=normalised_now,
        effective_as_of=resolve_effective_as_of(normalised_end),
        timeline=timeline_rows,
        runbook_steps_count=len(runbook_steps),
        followups=followups,
    )


def _project_timeline_rows(
    raw: dict[str, Any],
    *,
    default_as_of: datetime,
) -> list[PostmortemTimelineRow]:
    """Wrap raw :class:`TimelineEvent` dicts with citations.

    The citation's ``as_of`` is the event ts itself — the slice of the
    graph that the row was projected from.  Falls back to
    ``default_as_of`` (the ``incident_end`` anchor) when the wire row is
    malformed.
    """
    rows: list[PostmortemTimelineRow] = []
    events: list[dict[str, Any]] = raw.get("events") or []
    for event in events:
        ts = _parse_dt(event.get("ts")) or default_as_of
        entity_id = str(event.get("entity_id") or "")
        entity_type = str(event.get("entity_type") or "")
        source = str(event.get("source") or "")
        rows.append(
            PostmortemTimelineRow(
                ts=ts,
                entity_id=entity_id,
                entity_type=entity_type,
                change_kind=str(event.get("change_kind") or "created"),
                before_state_summary=event.get("before_state_summary"),
                after_state_summary=event.get("after_state_summary"),
                citation=Citation(
                    entity_id=entity_id,
                    entity_type=entity_type,
                    as_of=ts,
                    source=source,
                ),
            )
        )
    return rows


def _load_runbook_steps(
    app: FastAPI,
    *,
    workspace_id: uuid.UUID,
    incident_id: str,
) -> list[dict[str, Any]]:
    """Read recent runbook-step events for ``incident_id`` from the recorder.

    Returns ``[]`` when the recorder is absent (tests that don't wire one
    up) or when no events match the incident.  The recorder enforces
    workspace ACL inside :meth:`RunbookStepRecorder.recent`.
    """
    recorder = getattr(app.state, "runbook_step_recorder", None)
    if recorder is None:
        return []
    try:
        events = recorder.recent(workspace_id=workspace_id, limit=500)
    except Exception:  # pragma: no cover - defensive
        return []
    out: list[dict[str, Any]] = []
    for event in events:
        # ``RunbookStepEvent.to_payload()`` is the contract surface.
        if not hasattr(event, "to_payload"):
            continue
        payload = event.to_payload()
        if payload.get("incident_id") == incident_id:
            out.append(payload)
    return out


def _earliest_ts(rows: list[PostmortemTimelineRow], *, fallback: datetime) -> datetime:
    """Earliest event ts, or ``fallback`` when no events exist."""
    if not rows:
        return fallback
    return min(r.ts for r in rows)


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render(report: PostmortemReport, *, fmt: str = "markdown") -> str:
    """Render the report to ``markdown`` or ``html``.

    ``json`` is also accepted as a synonym for ``report.model_dump_json``
    so callers that want the raw structured form can use the same entry
    point.  Raises :class:`PostmortemError(INVALID_FORMAT_CODE, ...)` on
    unknown formats.
    """
    normalised = _validate_format(fmt)
    if normalised == "json":
        return report.model_dump_json(indent=2)
    template = get_template(report.template_id)
    if normalised == "markdown":
        return _render_markdown(report, template)
    return _render_html(report, template)


def _render_markdown(report: PostmortemReport, template: PostmortemTemplate) -> str:
    """Render the report as GitHub-flavoured markdown."""
    out: list[str] = []
    out.append(f"# Post-mortem: {report.incident_id}")
    out.append("")
    out.append(f"_Template: **{template.display_name}** ({template.template_id})_")
    out.append("")
    out.append(
        "| Field | Value |\n"
        "| --- | --- |\n"
        f"| Alert | `{report.alert_id}` |\n"
        f"| Incident start | {report.incident_start.isoformat()} |\n"
        f"| Incident end | {report.incident_end.isoformat()} |\n"
        f"| Effective as-of | {report.effective_as_of.isoformat()} |\n"
        f"| Generated at | {report.generated_at.isoformat()} |\n"
        f"| Timeline events | {len(report.timeline)} |\n"
        f"| Runbook steps recorded | {report.runbook_steps_count} |\n"
        f"| Follow-ups extracted | {len(report.followups)} |"
    )
    out.append("")
    for section in template.sections:
        out.extend(_render_section_markdown(section, report))
    out.append("")
    out.append("---")
    out.append(
        "_Generated by Omniscience post-mortem (#232) — bitemporal timeline "
        f"anchored at `as_of={report.effective_as_of.isoformat()}`._"
    )
    return "\n".join(out)


def _render_section_markdown(section: TemplateSection, report: PostmortemReport) -> list[str]:
    out: list[str] = [f"## {section.heading}", "", section.description, ""]
    if section.prompts:
        for prompt in section.prompts:
            out.append(f"- {prompt}")
        out.append("")
    if section.requires_timeline:
        out.extend(_render_timeline_markdown(report.timeline))
    if section.requires_followups:
        out.extend(_render_followups_markdown(report.followups))
    return out


def _render_timeline_markdown(rows: list[PostmortemTimelineRow]) -> list[str]:
    if not rows:
        return [
            "_(No timeline events fall within the incident window.  Widen "
            "`incident_start` / `incident_end` or check that the alert has "
            "neighbours with populated `valid_from`.)_",
            "",
        ]
    lines: list[str] = [
        "| # | Timestamp (UTC) | Change | Entity | Citation | Summary |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for idx, row in enumerate(rows, start=1):
        summary = (row.after_state_summary or row.before_state_summary or "").replace("|", "/")
        # Clip in-cell to keep tables readable in narrow renderers.
        summary = summary if len(summary) <= 160 else summary[:157] + "..."
        lines.append(
            f"| {idx} | {row.ts.isoformat()} | {row.change_kind} | "
            f"`{row.entity_type}/{row.entity_id}` | {row.citation.to_markdown()} | "
            f"{summary} |"
        )
    lines.append("")
    return lines


def _render_followups_markdown(followups: list[FollowUp]) -> list[str]:
    if not followups:
        return ["_(No follow-up action items extracted.)_", ""]
    lines: list[str] = []
    for f in followups:
        prov = ""
        if f.source_entity_id is not None:
            prov = f" — source `{f.source_entity_id}`"
            if f.source_as_of is not None:
                prov += f" @ {f.source_as_of.isoformat()}"
        lines.append(f"- [ ] **[{f.priority}]** {f.title}{prov}  ")
        lines.append(f"  _FollowUp entity: `{f.name}`_")
    lines.append("")
    return lines


def _render_html(report: PostmortemReport, template: PostmortemTemplate) -> str:
    """Render an HTML form of the report.

    Minimal, no-style HTML so embedders can drop their own CSS in.  Each
    output value is HTML-escaped; never trust connector strings.
    """
    parts: list[str] = []
    parts.append("<!doctype html>")
    parts.append("<html><head><meta charset='utf-8'>")
    parts.append(f"<title>Post-mortem: {html.escape(report.incident_id)}</title></head><body>")
    parts.append(f"<h1>Post-mortem: {html.escape(report.incident_id)}</h1>")
    parts.append(
        f"<p><em>Template: <strong>{html.escape(template.display_name)}</strong> "
        f"({html.escape(template.template_id)})</em></p>"
    )
    parts.append("<table border='1' cellpadding='4'>")
    parts.append(
        f"<tr><th>Alert</th><td><code>{html.escape(report.alert_id)}</code></td></tr>"
        f"<tr><th>Incident start</th><td>{report.incident_start.isoformat()}</td></tr>"
        f"<tr><th>Incident end</th><td>{report.incident_end.isoformat()}</td></tr>"
        f"<tr><th>Effective as-of</th><td>{report.effective_as_of.isoformat()}</td></tr>"
        f"<tr><th>Generated at</th><td>{report.generated_at.isoformat()}</td></tr>"
        f"<tr><th>Timeline events</th><td>{len(report.timeline)}</td></tr>"
        f"<tr><th>Runbook steps recorded</th><td>{report.runbook_steps_count}</td></tr>"
        f"<tr><th>Follow-ups extracted</th><td>{len(report.followups)}</td></tr>"
    )
    parts.append("</table>")
    for section in template.sections:
        parts.append(f"<h2>{html.escape(section.heading)}</h2>")
        parts.append(f"<p>{html.escape(section.description)}</p>")
        if section.prompts:
            parts.append("<ul>")
            for prompt in section.prompts:
                parts.append(f"<li>{html.escape(prompt)}</li>")
            parts.append("</ul>")
        if section.requires_timeline:
            parts.append(_render_timeline_html(report.timeline))
        if section.requires_followups:
            parts.append(_render_followups_html(report.followups))
    as_of_iso = html.escape(report.effective_as_of.isoformat())
    parts.append(
        "<hr><p><em>Generated by Omniscience post-mortem (#232) — bitemporal timeline "
        f"anchored at <code>as_of={as_of_iso}</code>.</em></p>"
    )
    parts.append("</body></html>")
    return "\n".join(parts)


def _render_timeline_html(rows: list[PostmortemTimelineRow]) -> str:
    if not rows:
        return "<p><em>No timeline events fall within the incident window.</em></p>"
    out: list[str] = [
        "<table border='1' cellpadding='4'><thead><tr>"
        "<th>#</th><th>Timestamp (UTC)</th><th>Change</th><th>Entity</th>"
        "<th>Citation</th><th>Summary</th></tr></thead><tbody>"
    ]
    for idx, row in enumerate(rows, start=1):
        summary = row.after_state_summary or row.before_state_summary or ""
        out.append(
            f"<tr><td>{idx}</td><td>{row.ts.isoformat()}</td>"
            f"<td>{html.escape(row.change_kind)}</td>"
            f"<td><code>{html.escape(row.entity_type)}/{html.escape(row.entity_id)}</code></td>"
            f"<td><code>{html.escape(row.entity_type)}/{html.escape(row.entity_id)}</code> "
            f"@ {row.citation.as_of.isoformat()}</td>"
            f"<td>{html.escape(summary)}</td></tr>"
        )
    out.append("</tbody></table>")
    return "\n".join(out)


def _render_followups_html(followups: list[FollowUp]) -> str:
    if not followups:
        return "<p><em>No follow-up action items extracted.</em></p>"
    out: list[str] = ["<ul>"]
    for f in followups:
        prov = ""
        if f.source_entity_id is not None:
            prov = f" &mdash; source <code>{html.escape(f.source_entity_id)}</code>" + (
                f" @ {f.source_as_of.isoformat()}" if f.source_as_of is not None else ""
            )
        out.append(
            f"<li><strong>[{html.escape(f.priority)}]</strong> "
            f"{html.escape(f.title)}{prov}"
            f"<br><small>FollowUp entity: <code>{html.escape(f.name)}</code></small></li>"
        )
    out.append("</ul>")
    return "\n".join(out)


__all__ = [
    "SUPPORTED_FORMATS",
    "Citation",
    "PostmortemFormat",
    "PostmortemReport",
    "PostmortemTimelineRow",
    "generate_postmortem",
    "render",
]
