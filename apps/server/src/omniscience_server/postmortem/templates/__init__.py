"""Post-mortem template registry.

Three default templates are shipped (issue #232 acceptance):

* :data:`BLAMELESS` — the Google SRE-style blameless post-mortem
  (Summary, Impact, Timeline, Root cause, Lessons, Action items).
* :data:`FIVE_WHYS` — Toyota-style iterative why-chain analysis, useful
  for incidents whose proximate cause is obvious but whose latent cause
  is not.
* :data:`COE` — AWS-style Correction of Errors document; expands the
  timeline section with explicit detection / mitigation / resolution
  buckets.

Templates are Pydantic models (not Jinja2 — see ``apps/server`` ADR
notes on dependency minimisation).  Section bodies are rendered by
:mod:`omniscience_server.postmortem.generator`; each template enumerates
the sections it wants emitted, in order.
"""

from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from omniscience_server.postmortem.errors import (
    INVALID_TEMPLATE_CODE,
    PostmortemError,
)

#: Closed Literal of template ids exposed on the public surface.  Adding
#: a template means extending this Literal AND registering the new
#: template in :data:`_REGISTRY`.
PostmortemTemplateId = Literal["blameless", "five_whys", "coe"]

#: The exact set of ids :class:`PostmortemTemplateId` permits.  Kept in
#: sync via the assertion below so that runtime checks stay aligned with
#: the type-level Literal.
SUPPORTED_TEMPLATES: Final[tuple[PostmortemTemplateId, ...]] = (
    "blameless",
    "five_whys",
    "coe",
)


class TemplateSection(BaseModel):
    """A single section of a post-mortem template."""

    model_config = ConfigDict(frozen=True)

    section_id: str = Field(
        description=(
            "Stable identifier (snake_case).  Used by callers that want "
            "to extract / re-order a specific section programmatically."
        ),
    )
    heading: str = Field(description="Human-readable heading (rendered as H2).")
    description: str = Field(
        description=(
            "Short prose shown immediately below the heading explaining "
            "what the section is for.  Helps responders fill it in."
        ),
    )
    requires_timeline: bool = Field(
        default=False,
        description=(
            "True iff the renderer should inject the cited timeline "
            "table into this section.  Exactly one section per template "
            "is expected to set this — the generator enforces it."
        ),
    )
    requires_followups: bool = Field(
        default=False,
        description=(
            "True iff the renderer should inject the extracted FollowUp "
            "checklist into this section.  As with timelines, exactly "
            "one section per template should set this."
        ),
    )
    prompts: list[str] = Field(
        default_factory=list,
        description=(
            "Bullet-list prompts that appear under the section "
            "description as a checklist of questions for the author."
        ),
    )


class PostmortemTemplate(BaseModel):
    """A complete post-mortem template — title + ordered section list."""

    model_config = ConfigDict(frozen=True)

    template_id: PostmortemTemplateId
    display_name: str = Field(description="Human-readable template name.")
    purpose: str = Field(description="One-sentence summary of when to use this template.")
    sections: list[TemplateSection] = Field(
        description="Ordered list of sections the renderer will emit.",
        min_length=1,
    )


# ---------------------------------------------------------------------------
# Default templates
# ---------------------------------------------------------------------------


BLAMELESS: Final[PostmortemTemplate] = PostmortemTemplate(
    template_id="blameless",
    display_name="Blameless post-mortem",
    purpose=(
        "Google SRE-style blameless review focused on systemic causes; "
        "the default template for most incidents."
    ),
    sections=[
        TemplateSection(
            section_id="summary",
            heading="Summary",
            description=(
                "One paragraph: what broke, who noticed, how long it "
                "lasted, and what the user-visible impact was."
            ),
        ),
        TemplateSection(
            section_id="impact",
            heading="Impact",
            description=(
                "Quantify the customer-visible effect — requests "
                "affected, error rate, revenue, SLO burn."
            ),
            prompts=[
                "How many users were affected?",
                "What was the error rate at peak?",
                "Was the SLO budget consumed?",
            ],
        ),
        TemplateSection(
            section_id="timeline",
            heading="Timeline",
            description=(
                "Chronological list of entity state changes between "
                "`incident_start` and `incident_end`, each row cited "
                "back to the entity and as-of timestamp it came from."
            ),
            requires_timeline=True,
        ),
        TemplateSection(
            section_id="root_cause",
            heading="Root cause",
            description=(
                "The systemic chain of contributing factors.  Blame "
                "code paths, configurations, and processes — not people."
            ),
            prompts=[
                "What was the trigger?",
                "What latent conditions allowed it to escalate?",
                "Why didn't existing safeguards catch it?",
            ],
        ),
        TemplateSection(
            section_id="lessons_learned",
            heading="Lessons learned",
            description=("What went well, what went wrong, where we got lucky. Bullet form."),
        ),
        TemplateSection(
            section_id="action_items",
            heading="Action items",
            description=(
                "Concrete follow-ups extracted from the incident.  Each "
                "becomes a FollowUp entity node in the graph for "
                "tracking."
            ),
            requires_followups=True,
        ),
    ],
)


FIVE_WHYS: Final[PostmortemTemplate] = PostmortemTemplate(
    template_id="five_whys",
    display_name="Five-whys analysis",
    purpose=(
        "Iterative why-chain analysis for incidents where the proximate "
        "cause is obvious but the latent cause is unclear."
    ),
    sections=[
        TemplateSection(
            section_id="problem_statement",
            heading="Problem statement",
            description="The user-visible symptom in one sentence.",
        ),
        TemplateSection(
            section_id="timeline",
            heading="Timeline",
            description=(
                "Chronological list of entity state changes during the "
                "incident window, each row cited."
            ),
            requires_timeline=True,
        ),
        TemplateSection(
            section_id="why_chain",
            heading="Why chain",
            description=(
                "Five iterations of 'why?' starting from the symptom.  "
                "Each answer becomes the next 'why?' until a systemic "
                "root cause is reached."
            ),
            prompts=[
                "Why 1: Why did the symptom occur?",
                "Why 2: Why did that condition exist?",
                "Why 3: Why was that condition not prevented?",
                "Why 4: Why did the prevention fail?",
                "Why 5: Why did the systemic safeguard not catch it?",
            ],
        ),
        TemplateSection(
            section_id="systemic_cause",
            heading="Systemic cause",
            description=(
                "The root cause exposed at the bottom of the why-chain. "
                "Typically a process or design gap, not a code bug."
            ),
        ),
        TemplateSection(
            section_id="countermeasures",
            heading="Countermeasures",
            description=(
                "Each systemic cause maps to one or more countermeasures "
                "that prevent the chain from recurring.  Tracked as "
                "FollowUp entities."
            ),
            requires_followups=True,
        ),
    ],
)


COE: Final[PostmortemTemplate] = PostmortemTemplate(
    template_id="coe",
    display_name="Correction of Errors (COE)",
    purpose=(
        "AWS-style COE document; expands the timeline with explicit "
        "detection / mitigation / resolution buckets and requires a "
        "lessons-learned section signed off by leadership."
    ),
    sections=[
        TemplateSection(
            section_id="summary",
            heading="Executive summary",
            description=(
                "Two-sentence summary for leadership: what happened, what is being done."
            ),
        ),
        TemplateSection(
            section_id="customer_impact",
            heading="Customer impact",
            description=(
                "Affected customers, regions, SKUs.  Quantify duration "
                "and severity using the SLO burn the timeline implies."
            ),
        ),
        TemplateSection(
            section_id="incident_response",
            heading="Incident response",
            description=(
                "Detection, mitigation, and resolution timestamps with "
                "the cited entity state-changes that map to each phase."
            ),
            requires_timeline=True,
            prompts=[
                "Detection: how was the incident first observed?",
                "Mitigation: what initial action reduced impact?",
                "Resolution: what action restored service to baseline?",
            ],
        ),
        TemplateSection(
            section_id="five_whys",
            heading="Five whys",
            description=(
                "COE format requires the abbreviated five-whys chain "
                "even when the root cause is documented in the next "
                "section.  Keeps the audit trail self-contained."
            ),
            prompts=["Why?"] * 5,
        ),
        TemplateSection(
            section_id="root_cause",
            heading="Root cause analysis",
            description=(
                "Document the primary contributing factor and any "
                "secondary factors discovered during investigation."
            ),
        ),
        TemplateSection(
            section_id="lessons_learned",
            heading="Lessons learned",
            description=(
                "What went well, what went poorly, what was lucky.  "
                "Requires sign-off from the on-call lead."
            ),
        ),
        TemplateSection(
            section_id="action_items",
            heading="Action items / corrective actions",
            description=(
                "Corrective actions extracted from the incident, each "
                "tracked as a FollowUp entity node."
            ),
            requires_followups=True,
        ),
    ],
)


_REGISTRY: Final[dict[PostmortemTemplateId, PostmortemTemplate]] = {
    "blameless": BLAMELESS,
    "five_whys": FIVE_WHYS,
    "coe": COE,
}

# Assert at import time that the Literal and registry agree.  Failing
# this assertion is a programmer error — we'd rather crash on import in
# a unit test than ship a Literal that lies about the registry.
if set(SUPPORTED_TEMPLATES) != set(_REGISTRY.keys()):  # pragma: no cover - import-time invariant
    raise RuntimeError(
        f"postmortem template registry/Literal mismatch: "
        f"SUPPORTED_TEMPLATES={SUPPORTED_TEMPLATES!r} "
        f"vs _REGISTRY keys={tuple(_REGISTRY.keys())!r}"
    )


def list_templates() -> list[PostmortemTemplate]:
    """Return every registered template (ordered as :data:`SUPPORTED_TEMPLATES`)."""
    return [_REGISTRY[tid] for tid in SUPPORTED_TEMPLATES]


def get_template(template_id: str) -> PostmortemTemplate:
    """Look up a template by id; raise :class:`PostmortemError` on miss.

    Accepts ``str`` (not :data:`PostmortemTemplateId`) so REST/MCP
    handlers can pass the raw wire string and get a structured error
    envelope back when the caller picked an unknown template.
    """
    if template_id not in _REGISTRY:
        raise PostmortemError(
            INVALID_TEMPLATE_CODE,
            f"template must be one of: {', '.join(SUPPORTED_TEMPLATES)}",
        )
    # mypy: narrow the Literal — we just asserted membership.
    return _REGISTRY[template_id]


__all__ = [
    "BLAMELESS",
    "COE",
    "FIVE_WHYS",
    "SUPPORTED_TEMPLATES",
    "PostmortemTemplate",
    "PostmortemTemplateId",
    "TemplateSection",
    "get_template",
    "list_templates",
]
