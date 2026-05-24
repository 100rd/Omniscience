"""Unit tests for the post-mortem generator package (issue #232).

Covers the pure / synchronous surfaces:

* Template registry shape + happy-path lookup.
* FollowUp extraction heuristics (trigger phrases, failed steps).
* The markdown / HTML / JSON renderer shape.
* Error mapping (PostmortemError carries a structured code).

Integration with the bitemporal timeline + REST endpoint lives in
``tests/integration/test_postmortem_generator.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from omniscience_server.postmortem import (
    INCIDENT_NOT_FOUND_CODE,
    INVALID_FORMAT_CODE,
    INVALID_INCIDENT_ID_CODE,
    INVALID_TEMPLATE_CODE,
    SUPPORTED_FORMATS,
    SUPPORTED_TEMPLATES,
    Citation,
    FollowUp,
    PostmortemError,
    PostmortemReport,
    PostmortemTimelineRow,
    extract_followups,
    get_template,
    list_templates,
    render,
)
from omniscience_server.postmortem.templates import BLAMELESS, COE, FIVE_WHYS

# ---------------------------------------------------------------------------
# Template registry
# ---------------------------------------------------------------------------


class TestTemplateRegistry:
    def test_supported_templates_matches_registry_keys(self) -> None:
        ids = {t.template_id for t in list_templates()}
        assert ids == set(SUPPORTED_TEMPLATES)
        assert ids == {"blameless", "five_whys", "coe"}

    def test_get_template_returns_pydantic_model(self) -> None:
        for tid in SUPPORTED_TEMPLATES:
            template = get_template(tid)
            assert template.template_id == tid
            assert len(template.sections) >= 1
            # Every template must include at least one section that
            # requires a timeline AND at least one that requires
            # follow-ups, so the renderer always has somewhere to put
            # the cited events + extracted action items.
            assert any(s.requires_timeline for s in template.sections)
            assert any(s.requires_followups for s in template.sections)

    def test_unknown_template_raises_invalid_template(self) -> None:
        with pytest.raises(PostmortemError) as excinfo:
            get_template("not-a-template")
        assert excinfo.value.code == INVALID_TEMPLATE_CODE

    def test_blameless_has_six_sections(self) -> None:
        assert len(BLAMELESS.sections) == 6
        assert [s.section_id for s in BLAMELESS.sections] == [
            "summary",
            "impact",
            "timeline",
            "root_cause",
            "lessons_learned",
            "action_items",
        ]

    def test_five_whys_has_prompts(self) -> None:
        # The why-chain section prompts power the renderer's bullet list.
        why_section = next(s for s in FIVE_WHYS.sections if s.section_id == "why_chain")
        assert len(why_section.prompts) == 5

    def test_coe_includes_executive_summary(self) -> None:
        assert COE.sections[0].section_id == "summary"
        assert "Executive" in COE.sections[0].heading


# ---------------------------------------------------------------------------
# FollowUp extraction
# ---------------------------------------------------------------------------


_ALERT = "alert://pagerduty/INC-T1"
_INCIDENT = "INC-T1"
_AS_OF = datetime(2026, 4, 12, 19, 30, tzinfo=UTC)


def _row(
    *,
    summary: str | None,
    entity_id: str = "pod/x",
    entity_type: str = "k8s_pod",
) -> PostmortemTimelineRow:
    return PostmortemTimelineRow(
        ts=_AS_OF,
        entity_id=entity_id,
        entity_type=entity_type,
        change_kind="created",
        before_state_summary=None,
        after_state_summary=summary,
        citation=Citation(
            entity_id=entity_id,
            entity_type=entity_type,
            as_of=_AS_OF,
            source="src-test",
        ),
    )


class TestFollowupExtraction:
    def test_baseline_always_includes_schedule_review(self) -> None:
        out = extract_followups(
            incident_id=_INCIDENT,
            alert_id=_ALERT,
            timeline=[],
            runbook_steps=[],
            now=_AS_OF,
        )
        assert len(out) == 1
        assert out[0].priority == "P3"
        assert "review" in out[0].title.lower()
        assert out[0].name.startswith("followup://inc-t1/")

    def test_todo_in_summary_promotes_to_followup(self) -> None:
        out = extract_followups(
            incident_id=_INCIDENT,
            alert_id=_ALERT,
            timeline=[_row(summary="TODO: tune retry backoff")],
            runbook_steps=[],
            now=_AS_OF,
        )
        titles = [f.title for f in out]
        assert any("TODO" in t for t in titles)
        # Provenance is set
        promoted = next(f for f in out if "TODO" in f.title)
        assert promoted.source_entity_id == "pod/x"
        assert promoted.source_as_of == _AS_OF

    def test_failed_runbook_step_promotes_to_followup(self) -> None:
        out = extract_followups(
            incident_id=_INCIDENT,
            alert_id=_ALERT,
            timeline=[],
            runbook_steps=[
                {
                    "outcome": "failed",
                    "step_id": "restart-db",
                    "runbook_name": "runbook://acme/db.md",
                    "valid_from": "2026-04-12T19:31:00+00:00",
                }
            ],
            now=_AS_OF,
        )
        titles = [f.title for f in out]
        assert any("failed" in t.lower() for t in titles)
        assert any("restart-db" in t for t in titles)

    def test_rolled_back_step_is_p1(self) -> None:
        out = extract_followups(
            incident_id=_INCIDENT,
            alert_id=_ALERT,
            timeline=[],
            runbook_steps=[
                {
                    "outcome": "rolled_back",
                    "step_id": "deploy-v2",
                    "runbook_name": "runbook://acme/deploy.md",
                }
            ],
            now=_AS_OF,
        )
        promoted = next(f for f in out if "deploy-v2" in f.title)
        assert promoted.priority == "P1"

    def test_succeeded_steps_are_ignored(self) -> None:
        out = extract_followups(
            incident_id=_INCIDENT,
            alert_id=_ALERT,
            timeline=[],
            runbook_steps=[
                {"outcome": "executed", "step_id": "ok", "runbook_name": "runbook://x/y.md"}
            ],
            now=_AS_OF,
        )
        # Only the baseline 'schedule review' should remain.
        assert len(out) == 1

    def test_duplicate_follow_ups_are_deduped(self) -> None:
        # Two timeline rows on the same entity_type with the same trigger
        # phrase produce a single FollowUp (canonical name collision).
        rows = [
            _row(summary="investigate retry storm", entity_id="pod/a"),
            _row(summary="investigate retry storm", entity_id="pod/b"),
        ]
        out = extract_followups(
            incident_id=_INCIDENT,
            alert_id=_ALERT,
            timeline=rows,
            runbook_steps=[],
            now=_AS_OF,
        )
        # one dedup'd 'investigate' + baseline 'schedule review'
        assert len(out) == 2


# ---------------------------------------------------------------------------
# Render shape
# ---------------------------------------------------------------------------


def _minimal_report(template_id: str = "blameless") -> PostmortemReport:
    row = _row(summary="PagerDuty: api pod 5xx spike")
    followup = FollowUp(
        name="followup://inc-t1/schedule-review",
        incident_id=_INCIDENT,
        alert_id=_ALERT,
        title="Schedule post-incident review meeting",
        priority="P3",
        source_entity_id=None,
        source_as_of=None,
        created_at=_AS_OF,
    )
    return PostmortemReport(
        incident_id=_INCIDENT,
        alert_id=_ALERT,
        template_id=template_id,  # type: ignore[arg-type]
        template_display_name=get_template(template_id).display_name,
        incident_start=_AS_OF,
        incident_end=_AS_OF,
        generated_at=_AS_OF,
        effective_as_of=_AS_OF,
        timeline=[row],
        runbook_steps_count=0,
        followups=[followup],
    )


class TestRender:
    def test_supported_formats_constant(self) -> None:
        assert set(SUPPORTED_FORMATS) == {"markdown", "html", "json"}

    def test_markdown_render_contains_template_title(self) -> None:
        for tid in SUPPORTED_TEMPLATES:
            md = render(_minimal_report(tid), fmt="markdown")
            assert md.startswith(f"# Post-mortem: {_INCIDENT}")
            assert get_template(tid).display_name in md

    def test_markdown_render_emits_timeline_table(self) -> None:
        md = render(_minimal_report(), fmt="markdown")
        assert "| # | Timestamp" in md
        # Citation is rendered with backticks + ISO timestamp.
        assert "`k8s_pod/pod/x`" in md
        assert _AS_OF.isoformat() in md

    def test_markdown_render_emits_followup_checklist(self) -> None:
        md = render(_minimal_report(), fmt="markdown")
        assert "- [ ] **[P3]** Schedule post-incident review meeting" in md
        assert "followup://inc-t1/schedule-review" in md

    def test_html_render_is_html_escaped(self) -> None:
        report = _minimal_report()
        row = report.timeline[0].model_copy(
            update={"after_state_summary": "<script>alert(1)</script>"}
        )
        report = report.model_copy(update={"timeline": [row]})
        html_out = render(report, fmt="html")
        assert "<script>" not in html_out
        assert "&lt;script&gt;" in html_out

    def test_json_render_is_valid_json(self) -> None:
        import json as _json

        rendered = render(_minimal_report(), fmt="json")
        parsed = _json.loads(rendered)
        assert parsed["incident_id"] == _INCIDENT
        assert parsed["template_id"] == "blameless"

    def test_invalid_format_raises_postmortem_error(self) -> None:
        with pytest.raises(PostmortemError) as excinfo:
            render(_minimal_report(), fmt="pdf")
        assert excinfo.value.code == INVALID_FORMAT_CODE


# ---------------------------------------------------------------------------
# PostmortemError envelope
# ---------------------------------------------------------------------------


class TestPostmortemError:
    def test_error_carries_code_attribute(self) -> None:
        exc = PostmortemError(INVALID_INCIDENT_ID_CODE, "empty")
        assert exc.code == INVALID_INCIDENT_ID_CODE
        assert str(exc) == "invalid_incident_id:empty"

    def test_error_is_value_error_subclass(self) -> None:
        # The existing REST/MCP error machinery pattern-matches ValueError.
        assert issubclass(PostmortemError, ValueError)

    def test_known_codes_are_stable(self) -> None:
        # Wire contract: these strings appear in client-side error
        # handlers; renaming them is a breaking change.
        assert INVALID_TEMPLATE_CODE == "invalid_template"
        assert INVALID_FORMAT_CODE == "invalid_format"
        assert INVALID_INCIDENT_ID_CODE == "invalid_incident_id"
        assert INCIDENT_NOT_FOUND_CODE == "incident_not_found"
