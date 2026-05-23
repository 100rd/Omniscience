"""Tests for the runbook connector + parser + linker (issue #231).

Coverage:

* :func:`parse_runbook` — well-formed runbook, missing front-matter, malformed
  front-matter (recoverable), unterminated code fence.
* Canonical name helpers — round-trip stability across source uids and paths.
* :func:`score_runbook_against_alert` — exact match, pattern match, tag
  overlap, severity, monotonicity invariant (more matching signals -> higher
  confidence), zero-signal -> 0.0 floor.
* :class:`RunbookConnector` — discover walks the fixture directory, fetch
  returns the parsed payload, validate rejects bad paths, registry lookup.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from omniscience_connectors import get_connector
from omniscience_connectors.base import DocumentRef
from omniscience_connectors.runbook import (
    RUNBOOK_NAME_PATTERN,
    RUNBOOK_STEP_NAME_PATTERN,
    ParseError,
    RunbookConfig,
    RunbookConnector,
    RunbookDocument,
    canonical_runbook_name,
    canonical_runbook_step_name,
    parse_runbook,
)
from omniscience_connectors.runbook.linker import (
    WEIGHT_EXACT,
    WEIGHT_PATTERN,
    WEIGHT_SEVERITY,
    WEIGHT_TAGS,
    AlertView,
    score_runbook_against_alert,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "runbook"


# ---------------------------------------------------------------------------
# Canonical name helpers
# ---------------------------------------------------------------------------


def test_canonical_runbook_name_basic() -> None:
    name = canonical_runbook_name("team-runbooks", "payments/db-conn.md")
    assert name == "runbook://team-runbooks/payments/db-conn.md"
    assert RUNBOOK_NAME_PATTERN.match(name)


def test_canonical_runbook_name_strips_leading_slashes() -> None:
    a = canonical_runbook_name("rb", "/sub/dir/file.md")
    b = canonical_runbook_name("rb", "sub/dir/file.md")
    c = canonical_runbook_name("rb", "./sub/dir/file.md")
    assert a == b == c == "runbook://rb/sub/dir/file.md"


def test_canonical_runbook_name_slugifies_source_uid() -> None:
    name = canonical_runbook_name("git://repo team", "p/file.md")
    assert name.startswith("runbook://git-repo-team/")


def test_canonical_runbook_step_name() -> None:
    step = canonical_runbook_step_name("rb", "p/f.md", "Restart the Pool")
    assert step == "runbook://rb/p/f.md#restart-the-pool"
    assert RUNBOOK_STEP_NAME_PATTERN.match(step)


def test_canonical_runbook_name_rejects_hash_in_path() -> None:
    with pytest.raises(ValueError, match="reserved for step suffix"):
        canonical_runbook_name("rb", "weird#name.md")


def test_canonical_step_name_rejects_unslugifiable() -> None:
    with pytest.raises(ValueError, match="empty string"):
        canonical_runbook_step_name("rb", "p/f.md", "!!!")


# ---------------------------------------------------------------------------
# parse_runbook — happy path
# ---------------------------------------------------------------------------


def test_parse_well_formed_runbook() -> None:
    text = (FIXTURE_DIR / "db-connections-exhausted.md").read_text()
    doc = parse_runbook(
        source_uid="team-runbooks",
        rel_path="db-connections-exhausted.md",
        text=text,
    )

    assert doc.title == "Database connections exhausted"
    assert doc.front_matter.alert_names == ["alert://datadog/db.connections.exhausted"]
    assert "alert://datadog/db.connections.*" in doc.front_matter.alert_patterns
    assert "alert://pagerduty/postgres-*" in doc.front_matter.alert_patterns
    assert "service:payments" in doc.front_matter.tags
    assert "service:checkout" in doc.front_matter.tags
    assert doc.front_matter.severity == "sev2"
    assert doc.front_matter.owners == ["team:payments"]
    assert doc.canonical_name == "runbook://team-runbooks/db-connections-exhausted.md"
    # 4 ATX headings in the fixture
    assert len(doc.steps) == 4
    step_ids = [s.step_id for s in doc.steps]
    assert step_ids == [
        "database-connections-exhausted",
        "triage",
        "mitigate",
        "resolve",
    ]
    # Code blocks inside steps are captured
    mitigate = next(s for s in doc.steps if s.step_id == "mitigate")
    assert any("kubectl" in cb for cb in mitigate.code_blocks)
    assert doc.parse_warnings == []


def test_parse_no_front_matter() -> None:
    text = (FIXTURE_DIR / "generic-5xx.md").read_text()
    doc = parse_runbook(
        source_uid="team-runbooks",
        rel_path="generic-5xx.md",
        text=text,
    )
    assert doc.title == "Generic 5xx spike"  # from first H1
    assert doc.front_matter.alert_names == []
    assert doc.front_matter.alert_patterns == []
    assert len(doc.steps) == 3
    assert doc.parse_warnings == []


def test_parse_malformed_front_matter_recovers() -> None:
    text = (FIXTURE_DIR / "malformed.md").read_text()
    doc = parse_runbook(
        source_uid="team-runbooks",
        rel_path="malformed.md",
        text=text,
    )
    # No closing fence => warning, body kept verbatim.
    assert any("front_matter" in w for w in doc.parse_warnings)
    # First real heading is still found.
    assert any(s.heading == "Real heading" for s in doc.steps)


def test_parse_empty_text_raises_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_runbook(source_uid="rb", rel_path="a.md", text="")


def test_parse_unterminated_code_fence_emits_warning() -> None:
    text = "# Heading\n\n```bash\nstart-of-code-block-no-end\n"
    doc = parse_runbook(source_uid="rb", rel_path="x.md", text=text)
    assert "unterminated_code_fence" in doc.parse_warnings


def test_parse_duplicate_headings_disambiguates() -> None:
    text = "# Same\nbody one\n\n# Same\nbody two\n"
    doc = parse_runbook(source_uid="rb", rel_path="x.md", text=text)
    assert [s.step_id for s in doc.steps] == ["same", "same-2"]
    assert any("duplicate_step_slug" in w for w in doc.parse_warnings)


def test_parse_inline_list_yaml() -> None:
    text = '---\ntags: ["a", "b", c]\nalert_names: [alert://x/1]\n---\n\n# Hi\n'
    doc = parse_runbook(source_uid="rb", rel_path="x.md", text=text)
    assert doc.front_matter.tags == ["a", "b", "c"]
    assert doc.front_matter.alert_names == ["alert://x/1"]


def test_parse_scalar_coerces_to_list() -> None:
    """A single string where a list is expected should be promoted."""
    text = "---\ntags: just-one-tag\n---\n\n# Hi\n"
    doc = parse_runbook(source_uid="rb", rel_path="x.md", text=text)
    assert doc.front_matter.tags == ["just-one-tag"]


# ---------------------------------------------------------------------------
# Linker / scoring
# ---------------------------------------------------------------------------


def _doc_with_fm(
    *,
    alert_names: list[str] | None = None,
    alert_patterns: list[str] | None = None,
    tags: list[str] | None = None,
    severity: str | None = None,
) -> RunbookDocument:
    body = "---\n"
    if alert_names:
        body += "alert_names:\n" + "\n".join(f"  - {n}" for n in alert_names) + "\n"
    if alert_patterns:
        body += "alert_patterns:\n" + "\n".join(f"  - {p}" for p in alert_patterns) + "\n"
    if tags:
        body += "tags:\n" + "\n".join(f"  - {t}" for t in tags) + "\n"
    if severity:
        body += f"severity: {severity}\n"
    body += "---\n\n# Test\nbody\n"
    return parse_runbook(source_uid="t", rel_path="t.md", text=body)


def test_score_exact_match_dominates() -> None:
    rb = _doc_with_fm(alert_names=["alert://dd/db.x"])
    alert = AlertView(name="alert://dd/db.x")
    result = score_runbook_against_alert(runbook=rb, alert=alert)
    assert result.exact_match is True
    # Exact-match alone -> weight_exact.
    assert result.confidence == pytest.approx(WEIGHT_EXACT)


def test_score_pattern_match() -> None:
    rb = _doc_with_fm(alert_patterns=["alert://dd/db.*"])
    alert = AlertView(name="alert://dd/db.connections.exhausted")
    result = score_runbook_against_alert(runbook=rb, alert=alert)
    assert result.pattern_match == "alert://dd/db.*"
    assert result.confidence == pytest.approx(WEIGHT_PATTERN)


def test_score_tag_jaccard() -> None:
    rb = _doc_with_fm(tags=["service:payments", "severity:sev2", "team:x"])
    alert = AlertView(
        name="alert://dd/some.alert",
        tags=["service:payments", "severity:sev2"],
    )
    result = score_runbook_against_alert(runbook=rb, alert=alert)
    # Intersection 2, union 3 -> jaccard 2/3.
    assert result.confidence == pytest.approx(WEIGHT_TAGS * (2 / 3))
    assert sorted(result.matched_tags) == ["service:payments", "severity:sev2"]


def test_score_severity_match() -> None:
    rb = _doc_with_fm(severity="sev1")
    alert = AlertView(name="alert://x/y", severity="sev1")
    result = score_runbook_against_alert(runbook=rb, alert=alert)
    assert result.severity_match is True
    assert result.confidence == pytest.approx(WEIGHT_SEVERITY)


def test_score_zero_signal_floors_to_zero() -> None:
    rb = _doc_with_fm()
    alert = AlertView(name="alert://x/y", tags=["service:other"], severity="sev3")
    result = score_runbook_against_alert(runbook=rb, alert=alert)
    assert result.confidence == 0.0


def test_score_monotonic_increase_with_more_signals() -> None:
    """The integration acceptance: more matching signals -> weakly higher confidence."""
    alert = AlertView(
        name="alert://dd/db.connections.exhausted",
        tags=["service:payments", "severity:sev2"],
        severity="sev2",
    )

    only_pattern = _doc_with_fm(alert_patterns=["alert://dd/db.*"])
    plus_tag = _doc_with_fm(
        alert_patterns=["alert://dd/db.*"],
        tags=["service:payments"],
    )
    plus_severity = _doc_with_fm(
        alert_patterns=["alert://dd/db.*"],
        tags=["service:payments"],
        severity="sev2",
    )
    plus_exact = _doc_with_fm(
        alert_names=["alert://dd/db.connections.exhausted"],
        alert_patterns=["alert://dd/db.*"],
        tags=["service:payments"],
        severity="sev2",
    )

    c_only = score_runbook_against_alert(runbook=only_pattern, alert=alert).confidence
    c_tag = score_runbook_against_alert(runbook=plus_tag, alert=alert).confidence
    c_sev = score_runbook_against_alert(runbook=plus_severity, alert=alert).confidence
    c_full = score_runbook_against_alert(runbook=plus_exact, alert=alert).confidence

    assert c_only < c_tag < c_sev < c_full
    assert c_full <= 1.0


# ---------------------------------------------------------------------------
# RunbookConnector
# ---------------------------------------------------------------------------


def test_connector_registry_lookup() -> None:
    instance = get_connector("runbook")
    assert isinstance(instance, RunbookConnector)
    assert RunbookConnector.connector_type == "runbook"
    assert RunbookConnector.config_schema is RunbookConfig


@pytest.mark.asyncio
async def test_connector_validate_rejects_missing_path(tmp_path: Path) -> None:
    cn = RunbookConnector()
    cfg = RunbookConfig(root_path=str(tmp_path / "does-not-exist"), source_uid="rb")
    with pytest.raises(ValueError, match="does not exist"):
        await cn.validate(cfg, {})


@pytest.mark.asyncio
async def test_connector_validate_rejects_non_directory(tmp_path: Path) -> None:
    f = tmp_path / "a.md"
    f.write_text("# x")
    cn = RunbookConnector()
    cfg = RunbookConfig(root_path=str(f), source_uid="rb")
    with pytest.raises(ValueError, match="not a directory"):
        await cn.validate(cfg, {})


@pytest.mark.asyncio
async def test_connector_discover_yields_only_markdown(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# A")
    (tmp_path / "b.markdown").write_text("# B")
    (tmp_path / "c.txt").write_text("not a runbook")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "d.md").write_text("# D")

    cn = RunbookConnector()
    cfg = RunbookConfig(root_path=str(tmp_path), source_uid="team-rb")
    refs = [ref async for ref in cn.discover(cfg, {})]
    paths = sorted(ref.metadata["path"] for ref in refs)
    assert paths == ["a.md", "b.markdown", "sub/d.md"]
    # Canonical names produced by discover() match the helper.
    for ref in refs:
        assert ref.uri.startswith("runbook://team-rb/")


@pytest.mark.asyncio
async def test_connector_fetch_returns_parsed_payload(tmp_path: Path) -> None:
    f = tmp_path / "a.md"
    f.write_text(
        "---\ntitle: T\nalert_names:\n  - alert://x/1\n---\n\n# Heading\nbody\n",
    )
    cn = RunbookConnector()
    cfg = RunbookConfig(root_path=str(tmp_path), source_uid="rb")
    refs = [ref async for ref in cn.discover(cfg, {})]
    assert len(refs) == 1
    fetched = await cn.fetch(cfg, {}, refs[0])
    assert fetched.content_type == "application/vnd.omniscience.runbook+json"
    payload = json.loads(fetched.content_bytes.decode("utf-8"))
    assert payload["title"] == "T"
    assert payload["front_matter"]["alert_names"] == ["alert://x/1"]
    assert payload["canonical_name"] == "runbook://rb/a.md"


@pytest.mark.asyncio
async def test_connector_fetch_rejects_path_outside_root(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# A")
    cn = RunbookConnector()
    cfg = RunbookConfig(root_path=str(tmp_path), source_uid="rb")
    rogue_ref = DocumentRef(
        external_id="/etc/passwd",
        uri="runbook://rb/etc/passwd",
        metadata={"path": "etc/passwd"},
    )
    with pytest.raises(ValueError, match="outside the configured root"):
        await cn.fetch(cfg, {}, rogue_ref)


@pytest.mark.asyncio
async def test_connector_no_webhook_handler() -> None:
    assert RunbookConnector().webhook_handler() is None


@pytest.mark.asyncio
async def test_connector_discover_respects_exclude_globs(tmp_path: Path) -> None:
    (tmp_path / "keep.md").write_text("# K")
    drafts = tmp_path / "drafts"
    drafts.mkdir()
    (drafts / "skip.md").write_text("# S")

    cn = RunbookConnector()
    cfg = RunbookConfig(
        root_path=str(tmp_path),
        source_uid="rb",
        exclude_globs=["drafts/*"],
    )
    refs = [ref async for ref in cn.discover(cfg, {})]
    paths = sorted(ref.metadata["path"] for ref in refs)
    assert paths == ["keep.md"]
