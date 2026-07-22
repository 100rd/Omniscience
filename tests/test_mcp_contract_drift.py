"""Offline skew checks for the stable MCP v1 contract publication."""

from __future__ import annotations

import importlib.util
import json
import shutil
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECKER_PATH = ROOT / "scripts" / "check_mcp_contract_drift.py"
SPEC = importlib.util.spec_from_file_location("check_mcp_contract_drift", CHECKER_PATH)
assert SPEC is not None and SPEC.loader is not None
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


def test_python_mcp_sdk_is_pinned_to_the_stable_v1_major() -> None:
    with (ROOT / "apps" / "server" / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]

    assert "mcp>=1.27,<2" in project["dependencies"]


def test_canary_http_client_is_a_direct_server_dependency() -> None:
    with (ROOT / "apps" / "server" / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]

    assert "httpx>=0.27.0" in project["dependencies"]

    with (ROOT / "uv.lock").open("rb") as handle:
        packages = tomllib.load(handle)["package"]
    locked = next(package for package in packages if package["name"] == "omniscience-server")
    requirements = {
        requirement["name"]: requirement.get("specifier")
        for requirement in locked["metadata"]["requires-dist"]
    }
    assert requirements["httpx"] == ">=0.27.0"
    assert requirements["mcp"] == ">=1.27,<2"


@pytest.mark.parametrize(
    ("relative", "old", "new", "expected"),
    [
        (
            Path("apps/server/pyproject.toml"),
            '"mcp>=1.27,<2"',
            '"mcp>=2"',
            "server dependency must pin the stable MCP SDK v1 line",
        ),
        (
            Path("apps/server/pyproject.toml"),
            '"httpx>=0.27.0"',
            '"httpx>=1"',
            "server dependency must declare the canary HTTP client",
        ),
        (
            Path("uv.lock"),
            '{ name = "mcp", specifier = ">=1.27,<2" }',
            '{ name = "mcp", specifier = ">=2" }',
            "lock metadata must pin the stable MCP SDK v1 line",
        ),
        (
            Path("uv.lock"),
            'name = "mcp"\nversion = "1.27.0"',
            'name = "mcp"\nversion = "2.0.0"',
            "resolved MCP SDK must stay on verified v1",
        ),
    ],
)
def test_offline_drift_gate_rejects_sdk_dependency_and_lock_skew(
    tmp_path: Path,
    relative: Path,
    old: str,
    new: str,
    expected: str,
) -> None:
    for source in (Path("apps/server/pyproject.toml"), Path("uv.lock")):
        target = tmp_path / source
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / source, target)

    errors: list[str] = []
    CHECKER._validate_sdk_dependencies(tmp_path, errors)
    assert errors == []

    target = tmp_path / relative
    content = target.read_text(encoding="utf-8")
    assert content.count(old) == 1
    target.write_text(content.replace(old, new), encoding="utf-8")
    errors = []
    CHECKER._validate_sdk_dependencies(tmp_path, errors)

    assert any(expected in error for error in errors)


def test_repository_drift_check_executes_sdk_dependency_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "sdk-validator-was-called"

    def inject_marker(_root: Path, errors: list[str]) -> None:
        errors.append(marker)

    monkeypatch.setattr(CHECKER, "_validate_sdk_dependencies", inject_marker)

    assert marker in CHECKER.check_repository(ROOT)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("project = []\n", "server dependency must pin"),
        ("project = [\n", "invalid or unreadable TOML"),
    ],
)
def test_sdk_dependency_gate_fails_closed_on_malformed_project_toml(
    tmp_path: Path,
    content: str,
    expected: str,
) -> None:
    project = tmp_path / CHECKER.SERVER_PROJECT
    project.parent.mkdir(parents=True)
    project.write_text(content, encoding="utf-8")
    lock = tmp_path / CHECKER.WORKSPACE_LOCK
    shutil.copy2(ROOT / CHECKER.WORKSPACE_LOCK, lock)

    errors: list[str] = []
    CHECKER._validate_sdk_dependencies(tmp_path, errors)

    assert any(expected in error for error in errors)


@pytest.mark.parametrize(
    ("relative", "old", "new"),
    [
        (
            CHECKER.MCP_DOC,
            "python -m omniscience_server.mcp.canary",
            "python -m broken.canary",
        ),
        (
            CHECKER.MCP_DOC,
            "--endpoint 'https://omniscience.example/mcp/'",
            "--endpoint 'https://omniscience.example/mcp'",
        ),
        (
            CHECKER.MCP_DOC,
            "`mcp>=1.27,<2`",
            "`mcp>=2`",
        ),
        (
            CHECKER.ROADMAP_DOC,
            "fail-closed portable canary verifier",
            "best-effort canary verifier",
        ),
    ],
)
def test_documentation_drift_gate_rejects_canary_publication_skew(
    tmp_path: Path,
    relative: Path,
    old: str,
    new: str,
) -> None:
    for source in (CHECKER.MCP_DOC, CHECKER.FRESHNESS_DOC, CHECKER.ROADMAP_DOC):
        target = tmp_path / source
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / source, target)

    errors: list[str] = []
    CHECKER._validate_docs(tmp_path, errors)
    assert errors == []

    target = tmp_path / relative
    content = target.read_text(encoding="utf-8")
    assert content.count(old) == 1
    target.write_text(content.replace(old, new), encoding="utf-8")
    errors = []
    CHECKER._validate_docs(tmp_path, errors)

    assert any("missing required fact" in error for error in errors)


def test_repository_contract_surfaces_are_in_sync() -> None:
    assert CHECKER.check_repository(ROOT) == []


def test_release_materializer_is_required_by_offline_drift_gate(tmp_path: Path) -> None:
    implementation = tmp_path / CHECKER.CONTRACT_IMPLEMENTATION
    materializer = tmp_path / CHECKER.RELEASE_MATERIALIZER
    canary = tmp_path / CHECKER.RUNTIME_CANARY
    implementation.parent.mkdir(parents=True)
    materializer.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / CHECKER.CONTRACT_IMPLEMENTATION, implementation)
    shutil.copy2(ROOT / CHECKER.RELEASE_MATERIALIZER, materializer)
    shutil.copy2(ROOT / CHECKER.RUNTIME_CANARY, canary)

    errors: list[str] = []
    CHECKER._validate_release_provenance(tmp_path, errors)
    assert errors == []

    materializer.unlink()
    errors = []
    CHECKER._validate_release_provenance(tmp_path, errors)
    assert any("mcp/materialize.py" in error for error in errors)

    shutil.copy2(ROOT / CHECKER.RELEASE_MATERIALIZER, materializer)
    canary.unlink()
    errors = []
    CHECKER._validate_release_provenance(tmp_path, errors)
    assert any("mcp/canary.py" in error for error in errors)


def test_catalog_gate_rejects_redirecting_streamable_http_endpoint(
    tmp_path: Path,
) -> None:
    for relative in (
        Path(".mcp/cline-marketplace.json"),
        Path(".mcp/pulsemcp.json"),
        Path(".mcp/registry.json"),
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    registry_path = tmp_path / ".mcp/registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["remotes"][0]["url"] = "https://{host}/mcp"
    registry_path.write_text(json.dumps(registry) + "\n", encoding="utf-8")

    errors: list[str] = []
    CHECKER._validate_catalogs(tmp_path, errors)

    assert any("canonical no-redirect endpoint" in error for error in errors)


@pytest.mark.parametrize("catalog", ["cline", "pulse", "registry"])
def test_catalog_gate_rejects_tool_skew_on_every_catalog(
    tmp_path: Path,
    catalog: str,
) -> None:
    paths = {
        "cline": Path(".mcp/cline-marketplace.json"),
        "pulse": Path(".mcp/pulsemcp.json"),
        "registry": Path(".mcp/registry.json"),
    }
    for relative in paths.values():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)

    errors: list[str] = []
    CHECKER._validate_catalogs(tmp_path, errors)
    assert errors == []

    target = tmp_path / paths[catalog]
    payload = json.loads(target.read_text(encoding="utf-8"))
    if catalog == "cline":
        content = payload["llmsInstallationContent"]
        marker = "MCP v1 tools (15): blast_radius"
        assert content.count(marker) == 1
        payload["llmsInstallationContent"] = content.replace(
            marker,
            "MCP v1 tools (15): unexpected_tool",
        )
    elif catalog == "pulse":
        payload["tools"][0]["name"] = "unexpected_tool"
    else:
        payload["_meta"]["io.github.100rd/omniscience"]["tools"][0] = "unexpected_tool"
    target.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    errors = []
    CHECKER._validate_catalogs(tmp_path, errors)

    assert any("unexpected tools: unexpected_tool" in error for error in errors)


@pytest.mark.parametrize(
    "surface",
    ["capability_index", "capability_spec", "task_index", "task_spec", "evidence"],
)
def test_governance_drift_gate_rejects_each_index_surface(
    tmp_path: Path,
    surface: str,
) -> None:
    execution_index = json.loads((ROOT / CHECKER.EXECUTION_INDEX).read_text(encoding="utf-8"))
    referenced_specs = {
        Path(entry["task_spec"])
        for entry in execution_index.get("entries", [])
        if isinstance(entry, dict) and isinstance(entry.get("task_spec"), str)
    }
    paths = {
        CHECKER.CAPABILITY_INDEX,
        CHECKER.CAPABILITY_SPEC,
        CHECKER.TASK_INDEX,
        CHECKER.EXECUTION_INDEX,
        *CHECKER.EXPECTED_TASKS,
        *referenced_specs,
    }
    for relative in paths:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)

    errors: list[str] = []
    CHECKER._validate_governance(tmp_path, errors)
    assert errors == []

    if surface == "capability_index":
        target = tmp_path / CHECKER.CAPABILITY_INDEX
        old = "[SPEC-MCP](SPEC-MCP-stable-contract-v1.md)"
        new = "[SPEC-MCP](missing.md)"
    elif surface == "capability_spec":
        target = tmp_path / CHECKER.CAPABILITY_SPEC
        old = "Status: ready"
        new = "Status: draft"
    elif surface == "task_index":
        target = tmp_path / CHECKER.TASK_INDEX
        old = "| [gh-issue-350-mcp-v1](gh-issue-350-mcp-contract-v1.md) | `ready` |"
        new = "| [gh-issue-350-mcp-v1](gh-issue-350-mcp-contract-v1.md) | `draft` |"
    elif surface == "task_spec":
        target = tmp_path / Path("docs/specs/gh-issue-350-mcp-contract-v1.md")
        old = "status: ready"
        new = "status: draft"
    else:
        target = tmp_path / CHECKER.EXECUTION_INDEX
        old = '"provenance": "reported-github"'
        new = '"provenance": "unverified-local"'
    content = target.read_text(encoding="utf-8")
    # The execution index now holds one entry per delivered task, so a
    # governance surface like `"provenance": "reported-github"` legitimately
    # appears more than once. Corrupting a single occurrence is enough to
    # prove the drift gate rejects it.
    assert content.count(old) >= 1
    target.write_text(content.replace(old, new, 1), encoding="utf-8")
    errors = []
    CHECKER._validate_governance(tmp_path, errors)

    assert errors, surface


def test_governance_requires_implemented_evidence_for_every_issue_350_slice(
    tmp_path: Path,
) -> None:
    execution_index = json.loads((ROOT / CHECKER.EXECUTION_INDEX).read_text(encoding="utf-8"))
    referenced_specs = {
        Path(entry["task_spec"])
        for entry in execution_index.get("entries", [])
        if isinstance(entry, dict) and isinstance(entry.get("task_spec"), str)
    }
    paths = {
        CHECKER.CAPABILITY_INDEX,
        CHECKER.CAPABILITY_SPEC,
        CHECKER.TASK_INDEX,
        CHECKER.EXECUTION_INDEX,
        *CHECKER.EXPECTED_TASKS,
        *referenced_specs,
    }
    for relative in paths:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)

    target = tmp_path / CHECKER.EXECUTION_INDEX
    payload = json.loads(target.read_text(encoding="utf-8"))
    entry = next(
        item
        for item in payload["entries"]
        if item.get("task_id") == "gh-issue-350-mcp-v1"
    )
    entry["status"] = "ready"
    target.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    errors: list[str] = []
    CHECKER._validate_governance(tmp_path, errors)

    assert any(
        "gh-issue-350-mcp-v1 must have adjacent implemented evidence" in error
        for error in errors
    )


def test_runtime_ast_uses_explicit_wire_names() -> None:
    source = """
@mcp_server.tool(name="search", description="retrieval")
async def search_impl():
    pass

@mcp_server.tool(name="replay_context")
async def replay_context_tool():
    pass
"""

    assert CHECKER.extract_runtime_tool_names(source) == ["replay_context", "search"]


def test_literal_contract_info_fields_are_extracted() -> None:
    source = """
def build_contract_info():
    return {"version": "1.0.0", "manifest": {}}
"""

    assert CHECKER.extract_function_return_keys(source, "build_contract_info") == {
        "manifest",
        "version",
    }


def test_contract_info_surface_comparison_reports_skew() -> None:
    errors: list[str] = []

    CHECKER.compare_contract_info_fields({"version", "unexpected"}, "fixture", errors)

    assert errors == [
        "fixture: missing contract_info fields: capabilities, commit, manifest, "
        "manifest_sha256, meta, schema_digests, schema_sha256, token_profile_id, "
        "tool_registry_sha256",
        "fixture: unexpected contract_info fields: unexpected",
    ]


def test_tool_surface_comparison_reports_missing_and_extra() -> None:
    errors: list[str] = []

    CHECKER.compare_tool_surface(
        expected=["contract_info", "search"],
        actual=["search", "unexpected"],
        surface="fixture",
        errors=errors,
    )

    assert errors == [
        "fixture: missing tools: contract_info",
        "fixture: unexpected tools: unexpected",
    ]


def test_manifest_validation_rejects_schema_digest_drift(tmp_path: Path) -> None:
    contract_root = tmp_path / "v1"
    schema_root = contract_root / "schemas"
    schema_root.mkdir(parents=True)
    registry_path = contract_root / "tool-registry.json"
    schema_path = schema_root / "response.schema.json"
    manifest_path = contract_root / "manifest.json"
    registry_path.write_text(
        json.dumps(
            {
                "contract_version": "1.0.0",
                "tools": [
                    {
                        "name": "search",
                        "required_scope": "search",
                        "response_schema": "schemas/response.schema.json",
                        "workspace_required": True,
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    schema_path.write_text('{"type":"object"}\n', encoding="utf-8")
    manifest_path.write_text(
        json.dumps(
            {
                "contract_version": "1.0.0",
                "source_commit": {
                    "environment_variable": "OMNISCIENCE_MCP_SOURCE_COMMIT",
                    "release_required": True,
                    "strategy": "runtime-injection",
                },
                "token_profile_id": "omniscience-mcp-read-v1",
                "schema_set_sha256": "0" * 64,
                "tool_count": 1,
                "tool_registry": {
                    "path": "tool-registry.json",
                    "sha256": CHECKER.sha256_file(registry_path),
                },
                "schemas": [
                    {
                        "path": "schemas/response.schema.json",
                        "sha256": "0" * 64,
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    errors: list[str] = []

    CHECKER.validate_manifest(contract_root, errors)

    assert any("digest mismatch" in error for error in errors)


@pytest.mark.parametrize("version", ["v0", "1.0", "latest", "1.0.0-draft"])
def test_only_stable_1_0_0_is_accepted(version: str) -> None:
    errors: list[str] = []

    CHECKER.validate_contract_version(version, "fixture", errors)

    assert errors == [f"fixture: expected stable MCP contract 1.0.0, found {version!r}"]
