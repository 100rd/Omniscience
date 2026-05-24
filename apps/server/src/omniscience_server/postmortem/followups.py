"""FollowUp entity extraction (issue #232 acceptance).

When a post-mortem is generated, action items that emerge from the
timeline are materialised as :class:`FollowUp` entity nodes.  Each
FollowUp carries:

* a canonical name (``followup://{incident_id}/{slug}``) so it slots into
  the bitemporal graph the same way runbook steps do;
* a link back to the incident + alert it originated from;
* a priority bucket so triage tools can sort backlog items.

Extraction heuristics
---------------------

The v0.1 extractor is intentionally simple and deterministic:

1. Every ``runbook-step`` event with outcome ``failed`` or ``rolled_back``
   produces a "investigate failure" FollowUp.
2. Every entity in the timeline whose ``after_state_summary`` contains
   any of the trigger phrases (``"TODO"``, ``"follow-up"``,
   ``"investigate"``, ``"flaky"``, ``"known issue"``) produces a
   FollowUp with the matched phrase highlighted.
3. The template's ``action_items`` section is always backfilled with a
   default "Schedule incident review" FollowUp so the generator never
   emits an empty action-items section.

A future PR (#232 v2) will plug an LLM-based extractor behind the same
interface.  Keeping the surface stable lets us swap the implementation
without breaking the renderer.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from omniscience_server.postmortem.models import PostmortemTimelineRow

#: Closed Literal of priorities — keeps the schema queryable.
FollowUpPriority = Literal["P0", "P1", "P2", "P3"]

#: Trigger phrases that promote a timeline row to a FollowUp.  Case-
#: insensitive; whole-word matching.
_TRIGGER_PHRASES: Final[tuple[str, ...]] = (
    "TODO",
    "follow-up",
    "followup",
    "investigate",
    "flaky",
    "known issue",
    "tech debt",
    "tech-debt",
)

_TRIGGER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(" + "|".join(re.escape(p) for p in _TRIGGER_PHRASES) + r")\b",
    re.IGNORECASE,
)

#: Maximum number of FollowUps the extractor will emit per incident.
#: Past this point we shed load — the responder is better served by a
#: short ranked list than a thousand-item backlog.
MAX_FOLLOWUPS: Final[int] = 50


def _slugify(value: str) -> str:
    """Reduce a free-form string to a path-safe slug.

    Matches the runbook step slug rules ``[A-Za-z0-9._-]+`` so FollowUp
    canonical names compose into the bitemporal graph cleanly.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    if not cleaned:
        cleaned = uuid.uuid4().hex[:12]
    return cleaned[:64].lower()


def canonical_followup_name(incident_id: str, slug: str) -> str:
    """Canonical ``followup://`` URI for graph storage."""
    return f"followup://{_slugify(incident_id)}/{_slugify(slug)}"


class FollowUp(BaseModel):
    """An action item extracted from the post-mortem.

    Action items materialise as graph entity nodes (kind=``followup``)
    when the generator is invoked through the REST/MCP surfaces; the
    extractor returns them as Pydantic models so the renderer and the
    integration tests can introspect the list without a graph round-trip.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(description="Canonical 'followup://{incident_id}/{slug}'.")
    incident_id: str = Field(description="The incident this follow-up belongs to.")
    alert_id: str = Field(description="The originating alert id (for back-reference).")
    title: str = Field(description="One-sentence summary of the action item.")
    priority: FollowUpPriority = Field(default="P2")
    source_entity_id: str | None = Field(
        default=None,
        description=(
            "Optional: the entity whose state-change produced this "
            "follow-up.  Lets the responder UI jump to provenance."
        ),
    )
    source_as_of: datetime | None = Field(
        default=None,
        description=(
            "Optional: the as-of timestamp of the source entity row "
            "(matches the timeline citation's anchor)."
        ),
    )
    created_at: datetime = Field(description="When the FollowUp was extracted (UTC).")


def extract_followups(
    *,
    incident_id: str,
    alert_id: str,
    timeline: list[PostmortemTimelineRow],
    runbook_steps: list[dict[str, object]],
    now: datetime | None = None,
) -> list[FollowUp]:
    """Extract :class:`FollowUp`\\ s from the incident's timeline + steps.

    Parameters
    ----------
    incident_id, alert_id:
        Identifiers stamped on every emitted FollowUp.
    timeline:
        The rendered timeline rows produced by the generator — each
        carries the cited ``after_state_summary`` the heuristic scans.
    runbook_steps:
        Recent runbook-step events for the incident, as dicts produced
        by :meth:`RunbookStepEvent.to_payload`.  Failed / rolled-back
        steps each promote to a FollowUp.
    now:
        Override for deterministic tests; defaults to ``datetime.now(UTC)``.
    """
    timestamp = now or datetime.now(UTC)
    out: list[FollowUp] = []

    # Heuristic 1: failed / rolled-back runbook steps.
    for step in runbook_steps:
        outcome = str(step.get("outcome") or "")
        if outcome not in {"failed", "rolled_back"}:
            continue
        step_id = str(step.get("step_id") or "step")
        runbook_name = str(step.get("runbook_name") or "")
        title = f"Investigate {outcome} step '{step_id}' from {runbook_name}"
        out.append(
            FollowUp(
                name=canonical_followup_name(incident_id, f"step-{step_id}-{outcome}"),
                incident_id=incident_id,
                alert_id=alert_id,
                title=title,
                priority="P1" if outcome == "rolled_back" else "P2",
                source_entity_id=runbook_name or None,
                source_as_of=_parse_dt(step.get("valid_from")),
                created_at=timestamp,
            )
        )

    # Heuristic 2: trigger phrases in timeline row summaries.
    for row in timeline:
        haystack = " ".join(
            v for v in (row.before_state_summary, row.after_state_summary) if v is not None
        )
        match = _TRIGGER_PATTERN.search(haystack)
        if not match:
            continue
        trigger = match.group(1)
        title = f"Address '{trigger}' flagged on {row.entity_type} {row.entity_id}"
        out.append(
            FollowUp(
                name=canonical_followup_name(incident_id, f"{row.entity_type}-{trigger}"),
                incident_id=incident_id,
                alert_id=alert_id,
                title=title,
                priority="P2",
                source_entity_id=row.entity_id,
                source_as_of=row.citation.as_of,
                created_at=timestamp,
            )
        )

    # Heuristic 3: always include the "schedule review" baseline.
    out.append(
        FollowUp(
            name=canonical_followup_name(incident_id, "schedule-review"),
            incident_id=incident_id,
            alert_id=alert_id,
            title="Schedule post-incident review meeting",
            priority="P3",
            source_entity_id=None,
            source_as_of=None,
            created_at=timestamp,
        )
    )

    # De-duplicate by canonical name; preserve first occurrence ordering.
    seen: set[str] = set()
    unique: list[FollowUp] = []
    for f in out:
        if f.name in seen:
            continue
        seen.add(f.name)
        unique.append(f)

    return unique[:MAX_FOLLOWUPS]


def _parse_dt(value: object) -> datetime | None:
    """Best-effort ISO-8601 parsing for the runbook-step payload."""
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


__all__ = [
    "MAX_FOLLOWUPS",
    "FollowUp",
    "FollowUpPriority",
    "canonical_followup_name",
    "extract_followups",
]
