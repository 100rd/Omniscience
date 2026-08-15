"""Public contract tests for the stable MCP v1 wire profile."""

from __future__ import annotations

import hashlib
import inspect
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

EXPECTED_TOOLS = [
    "blast_radius",
    "contract_info",
    "find_similar_incidents",
    "generate_postmortem",
    "get_document",
    "get_entity",
    "get_related_entities",
    "incident_timeline",
    "list_entities",
    "list_sources",
    "replay_context",
    "resolve_incident",
    "search",
    "source_stats",
    "suggest_runbook",
]

WORKSPACE_ID = uuid.UUID("aaaaaaaa-1111-2222-3333-444400000001")
FRESH_SOURCE_ID = uuid.UUID("aaaaaaaa-1111-2222-3333-444400000002")
STALE_SOURCE_ID = uuid.UUID("aaaaaaaa-1111-2222-3333-444400000003")
#: Frozen instant for the *freshness* assertions.  Every one of those calls
#: passes ``now=NOW`` explicitly, so a fixed value is exactly what they want.
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)

#: Issue time for the MCP read-token fixtures.  These must NOT use ``NOW``.
#: The token is checked by ``validate_mcp_read_token``, which the tool invokes
#: without a ``now=`` argument and therefore compares against the real wall
#: clock.  A token issued at a fixed 2026-07-15 and expiring 30 days later
#: stops validating on 2026-08-14 and every day after, so the suite turns red
#: on a calendar boundary rather than on a code change.
#:
#: Widening the delta is not an option: ``MCP_READ_MAX_LIFETIME`` caps this
#: profile at 30 days and the validator rejects anything longer with
#: ``expiry_exceeds_30_days``.  Issuing relative to the current instant keeps
#: the fixture inside both bounds indefinitely.
TOKEN_ISSUED_AT = datetime.now(UTC)
TOKEN_LIFETIME = timedelta(days=30)
SOURCE_COMMIT = "a" * 40


def test_oversized_tool_result_is_an_error_not_a_contract_invalid_success() -> None:
    from omniscience_server.mcp.server import _apply_mcp_budget

    oversized = {
        "meta": {"contract": {"version": "1.0.0"}},
        "hits": ["x" * 80_000],
    }

    with pytest.raises(ValueError, match=r"^response_too_large:"):
        _apply_mcp_budget(oversized)


@pytest.mark.parametrize("invalid_value", [float("nan"), object()])
def test_non_json_tool_result_is_a_stable_error(invalid_value: object) -> None:
    from omniscience_server.mcp.server import _apply_mcp_budget

    with pytest.raises(ValueError, match=r"^response_invalid:"):
        _apply_mcp_budget({"meta": {}, "value": invalid_value})


def test_packaged_manifest_is_content_addressed_and_registry_is_canonical() -> None:
    from omniscience_server.mcp.contract import (
        CONTRACT_VERSION,
        load_manifest,
        load_tool_registry,
        verify_contract_artifacts,
    )

    manifest = load_manifest()
    registry = load_tool_registry()

    assert CONTRACT_VERSION == "1.0.0"
    assert manifest["contract_version"] == CONTRACT_VERSION
    assert manifest["tool_count"] == 15
    assert [entry["name"] for entry in registry["tools"]] == EXPECTED_TOOLS
    assert registry["description_semantics"] == "non-normative"
    assert {entry["required_scope"] for entry in registry["tools"]} == {"search"}
    assert verify_contract_artifacts() == []


def test_all_registered_tools_match_registry_and_route_success_through_v1_meta() -> None:
    from omniscience_server.mcp.contract import contract_root, load_tool_registry
    from omniscience_server.mcp.server import mcp_server

    registered = mcp_server._tool_manager._tools
    expected = [entry["name"] for entry in load_tool_registry()["tools"]]

    assert sorted(registered) == expected
    for name, tool in registered.items():
        source = inspect.getsource(tool.fn)
        if name == "contract_info":
            assert "build_contract_info" in source
            assert "_require_v1_profile" in source
        else:
            assert "_require_v1_profile" in source
            assert "_finalize_success" in source

        entry = next(item for item in load_tool_registry()["tools"] if item["name"] == name)
        input_path = contract_root() / entry["input_schema"]
        assert json.loads(input_path.read_text()) == tool.parameters
        assert hashlib.sha256(input_path.read_bytes()).hexdigest() == entry["input_schema_sha256"]


def test_release_source_commit_is_injected_and_release_build_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from omniscience_server.mcp.contract import (
        effective_manifest_bytes,
        resolve_source_commit,
    )

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(effective_manifest_bytes(SOURCE_COMMIT))
    monkeypatch.setenv("OMNISCIENCE_MCP_SOURCE_COMMIT", SOURCE_COMMIT)
    monkeypatch.setenv("OMNISCIENCE_MCP_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("OMNISCIENCE_RELEASE_BUILD", "true")
    assert resolve_source_commit() == SOURCE_COMMIT

    monkeypatch.delenv("OMNISCIENCE_MCP_SOURCE_COMMIT")
    with pytest.raises(RuntimeError, match="contract_provenance_unavailable"):
        resolve_source_commit()

    monkeypatch.setenv("OMNISCIENCE_MCP_SOURCE_COMMIT", "0" * 40)
    with pytest.raises(RuntimeError, match="contract_provenance_unavailable"):
        resolve_source_commit()


def test_release_mode_rejects_env_only_unmaterialized_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omniscience_server.mcp.contract import resolve_source_commit

    monkeypatch.setenv("OMNISCIENCE_MCP_SOURCE_COMMIT", SOURCE_COMMIT)
    monkeypatch.setenv("OMNISCIENCE_RELEASE_BUILD", "true")
    monkeypatch.delenv("OMNISCIENCE_MCP_MANIFEST_PATH", raising=False)

    with pytest.raises(RuntimeError, match="contract_provenance_unavailable"):
        resolve_source_commit()


def test_release_mode_rejects_tampered_materialized_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from omniscience_server.mcp.contract import effective_manifest, resolve_source_commit

    manifest = effective_manifest(SOURCE_COMMIT)
    manifest["capabilities"]["auth"] = ["caller-selected"]
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OMNISCIENCE_MCP_SOURCE_COMMIT", SOURCE_COMMIT)
    monkeypatch.setenv("OMNISCIENCE_MCP_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("OMNISCIENCE_RELEASE_BUILD", "true")

    with pytest.raises(RuntimeError, match="contract_provenance_unavailable"):
        resolve_source_commit()


def test_release_mode_rejects_packaged_schema_or_registry_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from omniscience_server.mcp.contract import (
        effective_manifest_bytes,
        resolve_source_commit,
    )

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(effective_manifest_bytes(SOURCE_COMMIT))
    monkeypatch.setenv("OMNISCIENCE_MCP_SOURCE_COMMIT", SOURCE_COMMIT)
    monkeypatch.setenv("OMNISCIENCE_MCP_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("OMNISCIENCE_RELEASE_BUILD", "true")

    with (
        patch(
            "omniscience_server.mcp.contract.verify_contract_artifacts",
            return_value=["schema_sha256_mismatch:schemas/search.input.schema.json"],
        ),
        pytest.raises(RuntimeError, match="contract_provenance_unavailable"),
    ):
        resolve_source_commit()


def test_release_contract_info_serves_exact_materialized_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    from omniscience_server.mcp.contract import (
        build_contract_info,
        effective_manifest_bytes,
        manifest_sha256,
    )

    manifest_bytes = effective_manifest_bytes(SOURCE_COMMIT)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(manifest_bytes)
    monkeypatch.setenv("OMNISCIENCE_MCP_SOURCE_COMMIT", SOURCE_COMMIT)
    monkeypatch.setenv("OMNISCIENCE_MCP_MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("OMNISCIENCE_RELEASE_BUILD", "true")

    payload = build_contract_info(WORKSPACE_ID)

    assert payload["manifest"] == json.loads(manifest_bytes)
    assert payload["manifest_sha256"] == hashlib.sha256(manifest_bytes).hexdigest()
    assert manifest_sha256() == payload["manifest_sha256"]


def test_effective_manifest_digest_is_deterministic_and_commit_bound() -> None:
    from omniscience_server.mcp.contract import effective_manifest_bytes, manifest_sha256

    other_commit = "b" * 40
    assert effective_manifest_bytes(SOURCE_COMMIT) == effective_manifest_bytes(SOURCE_COMMIT)
    assert manifest_sha256(SOURCE_COMMIT) == manifest_sha256(SOURCE_COMMIT)
    assert manifest_sha256(SOURCE_COMMIT) != manifest_sha256(other_commit)


def test_unpinnable_development_provenance_requires_consumer_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omniscience_server.mcp.contract import build_contract_info
    from omniscience_server.mcp.metadata import SourceSnapshot, build_response_meta

    monkeypatch.delenv("OMNISCIENCE_MCP_SOURCE_COMMIT", raising=False)
    monkeypatch.delenv("OMNISCIENCE_RELEASE_BUILD", raising=False)

    contract_payload = build_contract_info(WORKSPACE_ID)
    data_meta = build_response_meta(
        tool_name="get_document",
        payload={"document": {"source_id": str(FRESH_SOURCE_ID)}},
        workspace_id=WORKSPACE_ID,
        source_snapshots={
            FRESH_SOURCE_ID: SourceSnapshot(
                source_id=FRESH_SOURCE_ID,
                last_sync_at=NOW - timedelta(seconds=5),
                freshness_sla_seconds=60,
            )
        },
        now=NOW,
    )

    assert contract_payload["commit"] == "0" * 40
    assert contract_payload["meta"]["fallback"] == {
        "required": True,
        "reason": "contract_mismatch",
    }
    assert data_meta["freshness"]["status"] == "fresh"
    assert data_meta["fallback"] == {
        "required": True,
        "reason": "contract_mismatch",
    }


@pytest.mark.parametrize(
    ("tool_name", "payload"),
    [
        ("get_document", {"document": {"source_id": str(FRESH_SOURCE_ID)}}),
        ("list_sources", {"sources": [{"id": str(FRESH_SOURCE_ID)}]}),
        ("source_stats", {"id": str(FRESH_SOURCE_ID)}),
    ],
)
def test_no_projection_read_tools_are_converged_without_projection_evidence(
    tool_name: str,
    payload: dict[str, object],
) -> None:
    """Postgres-only tools (NO_PROJECTION_READ_TOOLS) never need fallback.

    These three tools don't read a Neo4j/Qdrant projection, so even absent
    any explicit lag/watermark evidence in the payload they must report
    consistency "converged" and fallback.required False. Pins the contract
    behavior to the shared ``NO_PROJECTION_READ_TOOLS`` source of truth in
    ``mcp/tools.py`` rather than a duplicated string set in metadata.py.
    """
    from omniscience_server.mcp.metadata import SourceSnapshot, build_response_meta

    meta = build_response_meta(
        tool_name=tool_name,
        payload=payload,
        workspace_id=WORKSPACE_ID,
        source_snapshots={
            FRESH_SOURCE_ID: SourceSnapshot(
                source_id=FRESH_SOURCE_ID,
                last_sync_at=NOW - timedelta(seconds=5),
                freshness_sla_seconds=60,
            )
        },
        now=NOW,
        source_commit=SOURCE_COMMIT,
    )

    assert meta["consistency"]["status"] == "converged"
    assert meta["fallback"]["required"] is False


def test_projection_read_tool_without_evidence_is_unknown_and_needs_fallback() -> None:
    """Contrast case: a tool outside NO_PROJECTION_READ_TOOLS defaults to unknown.

    ``search`` reads the graph/vector projections, so without explicit
    lag/watermark evidence its consistency must stay "unknown" and fallback
    must be required — proving the converged short-circuit is scoped to the
    three Postgres-only tools, not a blanket default.
    """
    from omniscience_server.mcp.metadata import SourceSnapshot, build_response_meta

    meta = build_response_meta(
        tool_name="search",
        payload={"hits": [{"source_id": str(FRESH_SOURCE_ID)}]},
        workspace_id=WORKSPACE_ID,
        source_snapshots={
            FRESH_SOURCE_ID: SourceSnapshot(
                source_id=FRESH_SOURCE_ID,
                last_sync_at=NOW - timedelta(seconds=5),
                freshness_sla_seconds=60,
            )
        },
        now=NOW,
        source_commit=SOURCE_COMMIT,
    )

    assert meta["freshness"]["status"] == "fresh"
    assert meta["consistency"]["status"] == "unknown"
    assert meta["fallback"] == {"required": True, "reason": "consistency_unknown"}


def test_contract_info_exposes_pin_and_source_free_meta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omniscience_server.mcp.contract import build_contract_info, load_manifest

    monkeypatch.setenv("OMNISCIENCE_MCP_SOURCE_COMMIT", SOURCE_COMMIT)
    payload = build_contract_info(WORKSPACE_ID)

    assert payload["version"] == "1.0.0"
    assert payload["commit"] == SOURCE_COMMIT
    assert payload["manifest"]["source_commit"] == {"git_commit": SOURCE_COMMIT}
    assert len(payload["manifest_sha256"]) == 64
    assert len(payload["schema_sha256"]) == 64
    assert list(payload["schema_digests"]) == sorted(payload["schema_digests"])
    assert set(payload["schema_digests"]) == {
        record["path"] for record in load_manifest()["schemas"]
    }
    assert len(payload["tool_registry_sha256"]) == 64
    assert payload["token_profile_id"] == "omniscience-mcp-read-v1"
    assert payload["capabilities"]["freshness"] == [
        "fresh",
        "stale",
        "unknown",
        "degraded",
    ]
    assert payload["meta"]["workspace_id"] == str(WORKSPACE_ID)
    assert payload["meta"]["freshness"]["status"] == "unknown"
    assert payload["meta"]["freshness"]["reason"] == "source_free_contract_metadata"
    assert payload["meta"]["fallback"] == {"required": False, "reason": None}


@pytest.mark.asyncio
async def test_registered_contract_info_requires_exact_profile_and_uses_token_workspace() -> None:
    from omniscience_server.mcp.server import mcp_server

    token = SimpleNamespace(
        id=uuid.uuid4(),
        profile_id="omniscience-mcp-read-v1",
        scopes=["search"],
        workspace_id=WORKSPACE_ID,
        created_at=TOKEN_ISSUED_AT,
        expires_at=TOKEN_ISSUED_AT + TOKEN_LIFETIME,
        is_active=True,
        token_prefix="sk_read_",
    )
    tool = mcp_server._tool_manager._tools["contract_info"]
    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch("omniscience_server.mcp.server._record_tool_invocation") as record,
    ):
        payload = await tool.fn(None)

    assert payload["meta"]["workspace_id"] == str(WORKSPACE_ID)
    record.assert_called_once_with(tool_name="contract_info", token=token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"profile_id": None},
        {"scopes": ["search", "sources:read"]},
    ],
)
async def test_registered_contract_info_rejects_legacy_and_extra_scope_tokens(
    overrides: dict[str, object],
) -> None:
    from omniscience_server.mcp.server import mcp_server

    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "profile_id": "omniscience-mcp-read-v1",
        "scopes": ["search"],
        "workspace_id": WORKSPACE_ID,
        "created_at": TOKEN_ISSUED_AT,
        "expires_at": TOKEN_ISSUED_AT + TOKEN_LIFETIME,
        "is_active": True,
        "token_prefix": "sk_read_",
    }
    values.update(overrides)
    token = SimpleNamespace(**values)
    tool = mcp_server._tool_manager._tools["contract_info"]
    with (
        patch(
            "omniscience_server.mcp.server._resolve_token",
            new_callable=AsyncMock,
            return_value=token,
        ),
        pytest.raises(ValueError, match="forbidden"),
    ):
        await tool.fn(None)


def test_response_meta_uses_only_lineage_sources_and_marks_mixed_stale() -> None:
    from omniscience_server.mcp.metadata import SourceSnapshot, build_response_meta

    payload = {
        "query": "api errors",
        "hits": [
            {"source": {"id": str(FRESH_SOURCE_ID)}, "text": "fresh"},
            {"source": {"id": str(STALE_SOURCE_ID)}, "text": "stale"},
        ],
        "unrelated_source_id": "bbbbbbbb-1111-2222-3333-444400000004",
        "staleness_seconds": 4,
        "degraded_subsystems": ["neo4j"],
    }
    snapshots = {
        FRESH_SOURCE_ID: SourceSnapshot(
            source_id=FRESH_SOURCE_ID,
            last_sync_at=NOW - timedelta(seconds=30),
            freshness_sla_seconds=60,
        ),
        STALE_SOURCE_ID: SourceSnapshot(
            source_id=STALE_SOURCE_ID,
            last_sync_at=NOW - timedelta(seconds=120),
            freshness_sla_seconds=60,
        ),
    }

    meta = build_response_meta(
        tool_name="search",
        payload=payload,
        workspace_id=WORKSPACE_ID,
        source_snapshots=snapshots,
        now=NOW,
        source_commit=SOURCE_COMMIT,
    )

    assert meta["freshness"]["status"] == "stale"
    from omniscience_server.mcp.contract import tool_schema_sha256

    assert meta["contract"]["schema_sha256"] == tool_schema_sha256("search")
    assert meta["freshness"]["oldest_source_age_seconds"] == 120
    assert meta["freshness"]["stale_source_ids"] == [str(STALE_SOURCE_ID)]
    assert meta["consistency"] == {
        "status": "degraded",
        "degraded_subsystems": ["neo4j"],
        "projection_lag_versions": 0,
    }
    assert meta["fallback"] == {"required": True, "reason": "stale_source"}


def test_response_meta_marks_never_synced_and_missing_lineage_unknown() -> None:
    from omniscience_server.mcp.metadata import SourceSnapshot, build_response_meta

    never_synced = build_response_meta(
        tool_name="get_document",
        payload={"document": {"source_id": str(FRESH_SOURCE_ID)}},
        workspace_id=WORKSPACE_ID,
        source_snapshots={
            FRESH_SOURCE_ID: SourceSnapshot(
                source_id=FRESH_SOURCE_ID,
                last_sync_at=None,
                freshness_sla_seconds=60,
            )
        },
        now=NOW,
        source_commit=SOURCE_COMMIT,
    )
    missing_lineage = build_response_meta(
        tool_name="get_document",
        payload={"document": {"id": "doc-1"}},
        workspace_id=WORKSPACE_ID,
        source_snapshots={},
        now=NOW,
        source_commit=SOURCE_COMMIT,
    )

    assert never_synced["freshness"]["status"] == "unknown"
    assert never_synced["fallback"]["reason"] == "never_synced"
    assert missing_lineage["freshness"]["status"] == "unknown"
    assert missing_lineage["fallback"] == {
        "required": True,
        "reason": "missing_lineage",
    }


def test_response_freshness_age_is_a_conservative_integer() -> None:
    from omniscience_server.mcp.metadata import SourceSnapshot, build_response_meta

    meta = build_response_meta(
        tool_name="get_document",
        payload={"document": {"source_id": str(FRESH_SOURCE_ID)}},
        workspace_id=WORKSPACE_ID,
        source_snapshots={
            FRESH_SOURCE_ID: SourceSnapshot(
                source_id=FRESH_SOURCE_ID,
                last_sync_at=NOW - timedelta(seconds=5, microseconds=500_000),
                freshness_sla_seconds=60,
            )
        },
        now=NOW,
        source_commit=SOURCE_COMMIT,
    )

    assert type(meta["freshness"]["oldest_source_age_seconds"]) is int
    assert meta["freshness"]["oldest_source_age_seconds"] == 6


def test_list_sources_accepts_explicit_entry_ids_and_nested_consistency_evidence() -> None:
    from omniscience_server.mcp.metadata import SourceSnapshot, build_response_meta

    payload = {
        "sources": [{"id": str(FRESH_SOURCE_ID), "name": "repo"}],
        "unrelated": {"id": str(STALE_SOURCE_ID)},
        "meta": {
            "degraded_subsystems": ["qdrant"],
            "projection_lag_versions": 3,
            "staleness_seconds": 99,
        },
    }
    meta = build_response_meta(
        tool_name="list_sources",
        payload=payload,
        workspace_id=WORKSPACE_ID,
        source_snapshots={
            FRESH_SOURCE_ID: SourceSnapshot(
                source_id=FRESH_SOURCE_ID,
                last_sync_at=NOW - timedelta(seconds=5),
                freshness_sla_seconds=60,
            )
        },
        now=NOW,
        source_commit=SOURCE_COMMIT,
    )

    assert meta["freshness"]["status"] == "fresh"
    assert meta["consistency"]["degraded_subsystems"] == ["qdrant"]
    assert meta["consistency"]["projection_lag_versions"] == 3
    assert meta["fallback"] == {
        "required": True,
        "reason": "projection_divergence",
    }


def test_source_stats_extracts_actual_top_level_source_id() -> None:
    from omniscience_server.mcp.metadata import extract_used_source_ids

    assert extract_used_source_ids(
        "source_stats", {"source_id": str(FRESH_SOURCE_ID), "id": "not-a-uuid"}
    ) == {FRESH_SOURCE_ID}


def test_source_error_has_degraded_precedence_while_retaining_stale_ids() -> None:
    from omniscience_server.mcp.metadata import SourceSnapshot, build_response_meta

    payload = {"sources": [{"id": str(FRESH_SOURCE_ID)}]}
    degraded = build_response_meta(
        tool_name="list_sources",
        payload=payload,
        workspace_id=WORKSPACE_ID,
        source_snapshots={
            FRESH_SOURCE_ID: SourceSnapshot(
                source_id=FRESH_SOURCE_ID,
                last_sync_at=NOW - timedelta(seconds=5),
                freshness_sla_seconds=60,
                status="error",
            )
        },
        now=NOW,
        source_commit=SOURCE_COMMIT,
    )
    stale = build_response_meta(
        tool_name="list_sources",
        payload=payload,
        workspace_id=WORKSPACE_ID,
        source_snapshots={
            FRESH_SOURCE_ID: SourceSnapshot(
                source_id=FRESH_SOURCE_ID,
                last_sync_at=NOW - timedelta(seconds=120),
                freshness_sla_seconds=60,
                status="error",
            )
        },
        now=NOW,
        source_commit=SOURCE_COMMIT,
    )

    assert degraded["freshness"]["status"] == "degraded"
    assert degraded["fallback"]["reason"] == "source_degraded"
    assert stale["freshness"]["status"] == "degraded"
    assert stale["freshness"]["stale_source_ids"] == [str(FRESH_SOURCE_ID)]
    assert stale["freshness"]["degraded_source_ids"] == [str(FRESH_SOURCE_ID)]
    assert stale["fallback"]["reason"] == "source_degraded"


def test_unwired_projection_consistency_is_unknown_not_converged() -> None:
    from omniscience_server.mcp.metadata import SourceSnapshot, build_response_meta

    meta = build_response_meta(
        tool_name="search",
        payload={
            "hits": [{"source": {"id": str(FRESH_SOURCE_ID)}}],
            "degraded_subsystems": [],
            "staleness_seconds": None,
            "pinned_watermark": None,
        },
        workspace_id=WORKSPACE_ID,
        source_snapshots={
            FRESH_SOURCE_ID: SourceSnapshot(
                source_id=FRESH_SOURCE_ID,
                last_sync_at=NOW - timedelta(seconds=5),
                freshness_sla_seconds=60,
            )
        },
        now=NOW,
        source_commit=SOURCE_COMMIT,
    )

    assert meta["freshness"]["status"] == "fresh"
    assert meta["consistency"]["status"] == "unknown"
    assert meta["fallback"] == {
        "required": True,
        "reason": "consistency_unknown",
    }


def test_response_meta_honours_explicit_as_of_without_renaming_legacy_fields() -> None:
    from omniscience_server.mcp.metadata import SourceSnapshot, attach_response_meta

    as_of = NOW - timedelta(hours=1)
    payload = {
        "query": "historical",
        "hits": [{"source": {"id": str(FRESH_SOURCE_ID)}}],
        "staleness_seconds": 2,
    }
    response = attach_response_meta(
        tool_name="search",
        payload=payload,
        workspace_id=WORKSPACE_ID,
        source_snapshots={
            FRESH_SOURCE_ID: SourceSnapshot(
                source_id=FRESH_SOURCE_ID,
                last_sync_at=NOW - timedelta(seconds=10),
                freshness_sla_seconds=60,
            )
        },
        now=NOW,
        effective_as_of=as_of,
        source_commit=SOURCE_COMMIT,
    )

    assert response["query"] == "historical"
    assert response["hits"] == payload["hits"]
    assert response["staleness_seconds"] == 2
    assert response["meta"]["effective_as_of"] == as_of.isoformat().replace("+00:00", "Z")
    assert response["meta"]["freshness"]["evaluated_at"] == NOW.isoformat().replace("+00:00", "Z")
    assert response["meta"]["freshness"]["status"] == "unknown"
    assert response["meta"]["freshness"]["reason"] == "missing_lineage"
    assert response["meta"]["consistency"] == {
        "status": "unknown",
        "degraded_subsystems": [],
        "projection_lag_versions": 0,
    }
    assert response["meta"]["fallback"] == {
        "required": True,
        "reason": "missing_lineage",
    }


def test_all_success_schemas_require_top_level_meta() -> None:
    from omniscience_server.mcp.contract import contract_root, load_manifest

    manifest = load_manifest()
    for schema_record in manifest["schemas"]:
        schema = json.loads((contract_root() / schema_record["path"]).read_text())
        if schema_record["path"].endswith("response.schema.json"):
            directly_requires_meta = "meta" in schema.get("required", [])
            inherits_response_envelope = {"$ref": "response.schema.json"} in schema.get(
                "allOf", []
            )
            assert directly_requires_meta or inherits_response_envelope


def test_every_registered_tool_response_validates_against_its_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jsonschema import Draft202012Validator
    from omniscience_server.mcp.contract import (
        build_contract_info,
        contract_root,
        load_manifest,
        load_tool_registry,
    )
    from omniscience_server.mcp.metadata import build_response_meta
    from referencing import Registry, Resource

    monkeypatch.setenv("OMNISCIENCE_MCP_SOURCE_COMMIT", SOURCE_COMMIT)
    manifest = load_manifest()
    schemas = {
        record["path"]: json.loads((contract_root() / record["path"]).read_text())
        for record in manifest["schemas"]
    }
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema))
        for schema in schemas.values()
        if "$id" in schema
    )
    for tool in load_tool_registry()["tools"]:
        if tool["name"] == "contract_info":
            payload = build_contract_info(WORKSPACE_ID)
        else:
            schema = schemas[tool["response_schema"]]
            payload = {}
            for key in schema["required"]:
                if key == "meta":
                    continue
                property_schema = schema["properties"][key]
                declared = property_schema.get("type")
                value_type = (
                    next(
                        (item for item in declared if item != "null"),
                        "string",
                    )
                    if isinstance(declared, list)
                    else declared
                )
                if value_type == "array":
                    payload[key] = []
                elif value_type == "object":
                    payload[key] = {}
                elif value_type == "integer":
                    payload[key] = 0
                elif value_type == "number":
                    payload[key] = 0.0
                elif value_type == "boolean":
                    payload[key] = False
                elif property_schema.get("format") == "date-time":
                    payload[key] = NOW.isoformat().replace("+00:00", "Z")
                elif property_schema.get("format") == "uuid":
                    payload[key] = str(FRESH_SOURCE_ID)
                else:
                    payload[key] = "value"
            payload["meta"] = build_response_meta(
                tool_name=tool["name"],
                payload=payload,
                workspace_id=WORKSPACE_ID,
                source_snapshots={},
                now=NOW,
                source_commit=SOURCE_COMMIT,
            )
        validator = Draft202012Validator(
            schemas[tool["response_schema"]],
            registry=registry,
        )
        assert list(validator.iter_errors(payload)) == [], tool["name"]
