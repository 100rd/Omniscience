#!/usr/bin/env python3
"""Fail offline when MCP v1 runtime, publication, docs, and governance drift."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_VERSION = "1.0.0"
TOKEN_PROFILE_ID = "omniscience-mcp-read-v1"  # noqa: S105 - public profile id
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
EXPECTED_CONTRACT_INFO_FIELDS = {
    "capabilities",
    "commit",
    "manifest",
    "manifest_sha256",
    "meta",
    "schema_digests",
    "schema_sha256",
    "token_profile_id",
    "tool_registry_sha256",
    "version",
}

CONTRACT_ROOT = Path("apps/server/src/omniscience_server/mcp/contracts/v1")
CONTRACT_IMPLEMENTATION = Path("apps/server/src/omniscience_server/mcp/contract.py")
RELEASE_MATERIALIZER = Path("apps/server/src/omniscience_server/mcp/materialize.py")
RUNTIME_CANARY = Path("apps/server/src/omniscience_server/mcp/canary.py")
RUNTIME_SERVER = Path("apps/server/src/omniscience_server/mcp/server.py")
SERVER_PROJECT = Path("apps/server/pyproject.toml")
WORKSPACE_LOCK = Path("uv.lock")
MCP_DOC = Path("docs/api/mcp.md")
FRESHNESS_DOC = Path("docs/freshness-and-lineage.md")
ROADMAP_DOC = Path("docs/roadmap.md")
CAPABILITY_INDEX = Path("specs/SPEC-INDEX.md")
CAPABILITY_SPEC = Path("specs/SPEC-MCP-stable-contract-v1.md")
TASK_INDEX = Path("docs/specs/README.md")
EXECUTION_INDEX = Path("docs/specs/execution-index.json")

EXPECTED_TASKS = {
    Path("docs/specs/gh-issue-350-mcp-contract-v1.md"): ("gh-issue-350-mcp-v1", "ready"),
    Path("docs/specs/gh-issue-350-production-ha.md"): ("gh-issue-350-production-ha", "ready"),
    Path("docs/specs/gh-issue-350-backup-restore.md"): (
        "gh-issue-350-backup-restore",
        "ready",
    ),
    Path("docs/specs/gh-issue-350-read-scaling-priority.md"): (
        "gh-issue-350-read-scaling-priority",
        "ready",
    ),
    Path("docs/specs/gh-issue-350-consumer-severance.md"): (
        "gh-issue-350-consumer-severance",
        "ready",
    ),
}

MCP_SDK_REQUIREMENT = "mcp>=1.27,<2"
HTTPX_REQUIREMENT = "httpx>=0.27.0"

# Evidence provenance kinds this governance gate trusts. Local/unverified
# claims must never satisfy execution-index governance; extend this set only
# when a new automated, independently-checkable evidence source is wired up.
_TRUSTED_VERIFICATION_PROVENANCE = frozenset({"reported-github"})
_TERMINAL_EVIDENCE_STATUSES = frozenset({"implemented", "verified"})


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_text(path: Path, errors: list[str]) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"{path}: cannot read: {exc}")
        return ""


def _load_json(path: Path, errors: list[str]) -> Any:
    text = _read_text(path, errors)
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        errors.append(f"{path}: invalid JSON: {exc}")
        return None


def _load_toml(path: Path, errors: list[str]) -> dict[str, Any] | None:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        errors.append(f"{path}: invalid or unreadable TOML: {exc}")
        return None


def validate_contract_version(version: object, surface: str, errors: list[str]) -> None:
    if version != CONTRACT_VERSION:
        errors.append(
            f"{surface}: expected stable MCP contract {CONTRACT_VERSION}, found {version!r}"
        )


def compare_tool_surface(
    *, expected: list[str], actual: list[str], surface: str, errors: list[str]
) -> None:
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    if missing:
        errors.append(f"{surface}: missing tools: {', '.join(missing)}")
    if extra:
        errors.append(f"{surface}: unexpected tools: {', '.join(extra)}")
    if not missing and not extra and actual != expected:
        errors.append(f"{surface}: tool registry is not in canonical sorted order")
    if len(actual) != len(set(actual)):
        errors.append(f"{surface}: duplicate tool names")


def extract_runtime_tool_names(source: str) -> list[str]:
    tree = ast.parse(source)
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            function = decorator.func
            if not isinstance(function, ast.Attribute) or function.attr != "tool":
                continue
            wire_name = node.name
            for keyword in decorator.keywords:
                if (
                    keyword.arg == "name"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    wire_name = keyword.value.value
            names.append(wire_name)
    return sorted(names)


def extract_function_return_keys(source: str, function_name: str) -> set[str]:
    """Return string keys from one function's literal response dictionary."""
    tree = ast.parse(source)
    function = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
        ),
        None,
    )
    if function is None:
        return set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Dict):
            continue
        keys = {
            key.value
            for key in node.value.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        if keys:
            return keys
    return set()


def compare_contract_info_fields(actual: set[str], surface: str, errors: list[str]) -> None:
    """Reject handshake fields missing from or added to one pinned surface."""
    missing = sorted(EXPECTED_CONTRACT_INFO_FIELDS - actual)
    extra = sorted(actual - EXPECTED_CONTRACT_INFO_FIELDS)
    if missing:
        errors.append(f"{surface}: missing contract_info fields: {', '.join(missing)}")
    if extra:
        errors.append(f"{surface}: unexpected contract_info fields: {', '.join(extra)}")


def _manifest_record_digest(record: dict[str, Any]) -> str | None:
    digest = record.get("sha256")
    return digest if isinstance(digest, str) else None


def validate_manifest(contract_root: Path, errors: list[str]) -> None:
    manifest_path = contract_root / "manifest.json"
    manifest = _load_json(manifest_path, errors)
    if not isinstance(manifest, dict):
        return

    validate_contract_version(manifest.get("contract_version"), str(manifest_path), errors)
    if manifest.get("tool_count") != len(EXPECTED_TOOLS):
        errors.append(
            f"{manifest_path}: tool_count must be {len(EXPECTED_TOOLS)}, "
            f"found {manifest.get('tool_count')!r}"
        )
    expected_commit_descriptor = {
        "environment_variable": "OMNISCIENCE_MCP_SOURCE_COMMIT",
        "release_required": True,
        "strategy": "runtime-injection",
    }
    if manifest.get("source_commit") != expected_commit_descriptor:
        errors.append(
            f"{manifest_path}: source_commit must use the fail-closed release-injection descriptor"
        )
    if manifest.get("token_profile_id") != TOKEN_PROFILE_ID:
        errors.append(f"{manifest_path}: token_profile_id must be {TOKEN_PROFILE_ID}")

    registry_record = manifest.get("tool_registry")
    if not isinstance(registry_record, dict):
        errors.append(f"{manifest_path}: tool_registry digest record is required")
    else:
        relative_path = registry_record.get("path")
        if relative_path != "tool-registry.json":
            errors.append(f"{manifest_path}: tool_registry path must be tool-registry.json")
        registry_path = contract_root / str(relative_path)
        if registry_path.is_file():
            expected_digest = _manifest_record_digest(registry_record)
            actual_digest = sha256_file(registry_path)
            if expected_digest != actual_digest:
                errors.append(
                    f"{manifest_path}: tool registry digest mismatch: "
                    f"expected {expected_digest!r}, computed {actual_digest}"
                )
        else:
            errors.append(f"{manifest_path}: missing {registry_path}")

    schema_records = manifest.get("schemas")
    if not isinstance(schema_records, list):
        errors.append(f"{manifest_path}: schemas must be a list of digest records")
        return

    recorded_paths: list[str] = []
    for record in schema_records:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            errors.append(f"{manifest_path}: malformed schema digest record")
            continue
        relative_path = record["path"]
        recorded_paths.append(relative_path)
        schema_path = contract_root / relative_path
        if not schema_path.is_file():
            errors.append(f"{manifest_path}: missing schema {schema_path}")
            continue
        expected_digest = _manifest_record_digest(record)
        actual_digest = sha256_file(schema_path)
        if expected_digest != actual_digest:
            errors.append(
                f"{manifest_path}: schema {relative_path} digest mismatch: "
                f"expected {expected_digest!r}, computed {actual_digest}"
            )

    disk_paths = sorted(
        str(path.relative_to(contract_root)) for path in (contract_root / "schemas").glob("*.json")
    )
    if recorded_paths != sorted(recorded_paths):
        errors.append(f"{manifest_path}: schema records must be sorted by path")
    if sorted(recorded_paths) != disk_paths:
        errors.append(
            f"{manifest_path}: schema index differs from packaged schemas "
            f"(manifest={sorted(recorded_paths)!r}, disk={disk_paths!r})"
        )
    schema_rows = [
        f"{record['path']}:{sha256_file(contract_root / record['path'])}"
        for record in sorted(schema_records, key=lambda item: item.get("path", ""))
        if isinstance(record, dict)
        and isinstance(record.get("path"), str)
        and (contract_root / record["path"]).is_file()
    ]
    aggregate = hashlib.sha256(("\n".join(schema_rows) + "\n").encode()).hexdigest()
    if manifest.get("schema_set_sha256") != aggregate:
        errors.append(
            f"{manifest_path}: schema set digest mismatch: "
            f"expected {manifest.get('schema_set_sha256')!r}, computed {aggregate}"
        )


def _validate_registry(contract_root: Path, errors: list[str]) -> list[str]:
    registry_path = contract_root / "tool-registry.json"
    registry = _load_json(registry_path, errors)
    if not isinstance(registry, dict):
        return []
    validate_contract_version(registry.get("contract_version"), str(registry_path), errors)
    if registry.get("description_semantics") != "non-normative":
        errors.append(f"{registry_path}: description_semantics must be non-normative")
    records = registry.get("tools")
    if not isinstance(records, list):
        errors.append(f"{registry_path}: tools must be a list")
        return []
    names: list[str] = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("name"), str):
            errors.append(f"{registry_path}: every tool needs a string name")
            continue
        names.append(record["name"])
        if record.get("required_scope") != "search":
            errors.append(f"{registry_path}: {record['name']} must require exact search scope")
        if record.get("workspace_required") is not True:
            errors.append(f"{registry_path}: {record['name']} must require a workspace")
        input_schema = record.get("input_schema")
        input_digest = record.get("input_schema_sha256")
        if not isinstance(input_schema, str) or not (contract_root / input_schema).is_file():
            errors.append(f"{registry_path}: {record['name']} references a missing input schema")
        elif input_digest != sha256_file(contract_root / input_schema):
            errors.append(f"{registry_path}: {record['name']} input schema digest mismatch")
        response_schema = record.get("response_schema")
        if not isinstance(response_schema, str) or not (contract_root / response_schema).is_file():
            errors.append(
                f"{registry_path}: {record['name']} references a missing response schema"
            )
    compare_tool_surface(
        expected=EXPECTED_TOOLS,
        actual=names,
        surface=str(registry_path),
        errors=errors,
    )
    return names


def _validate_contract_info_surface(root: Path, errors: list[str]) -> None:
    implementation = _read_text(root / CONTRACT_IMPLEMENTATION, errors)
    if implementation:
        compare_contract_info_fields(
            extract_function_return_keys(implementation, "build_contract_info"),
            str(CONTRACT_IMPLEMENTATION),
            errors,
        )

    schema_path = root / CONTRACT_ROOT / "schemas/contract-info.response.schema.json"
    schema = _load_json(schema_path, errors)
    if isinstance(schema, dict):
        required = schema.get("required")
        compare_contract_info_fields(
            set(required) if isinstance(required, list) else set(),
            str(schema_path.relative_to(root)),
            errors,
        )

    doc = _read_text(root / MCP_DOC, errors)
    marker = "`contract_info` is authenticated and returns:"
    try:
        example = doc.split(marker, 1)[1].split("```json", 1)[1].split("```", 1)[0]
        payload = json.loads(example)
    except (IndexError, json.JSONDecodeError) as exc:
        errors.append(f"{MCP_DOC}: cannot parse contract_info JSON example: {exc}")
    else:
        compare_contract_info_fields(set(payload), str(MCP_DOC), errors)


def _validate_release_provenance(root: Path, errors: list[str]) -> None:
    implementation = _read_text(root / CONTRACT_IMPLEMENTATION, errors)
    for fact in (
        "def _release_manifest_snapshot() -> tuple[dict[str, Any], bytes]:",
        "def resolve_source_commit() -> str:",
        'os.environ.get("OMNISCIENCE_MCP_SOURCE_COMMIT"',
        'os.environ.get("OMNISCIENCE_MCP_MANIFEST_PATH"',
        'os.environ.get("OMNISCIENCE_RELEASE_BUILD"',
        "if verify_contract_artifacts():",
        'raise RuntimeError("contract_provenance_unavailable")',
        '_DEVELOPMENT_COMMIT = "0" * 40',
    ):
        _require_text(implementation, fact, CONTRACT_IMPLEMENTATION, errors)

    materializer = _read_text(root / RELEASE_MATERIALIZER, errors)
    for fact in (
        "def materialize_release_bundle(",
        'raise _fail("source_commit_not_head")',
        'raise _fail("source_worktree_not_clean")',
        'raise _fail("schema_sha256_mismatch")',
        'bundle_dir = output_root / "sha256" / manifest_digest',
    ):
        _require_text(materializer, fact, RELEASE_MATERIALIZER, errors)

    canary = _read_text(root / RUNTIME_CANARY, errors)
    for fact in (
        "def load_release_pin(bundle_dir: Path) -> ReleasePin:",
        "def verify_runtime_observation(",
        "async def run_canary(",
        'os.environ.get("OMNISCIENCE_MCP_CANARY_TOKEN"',
        "follow_redirects=False",
        "trust_env=False",
        'raise _fail("manifest_address_mismatch")',
        'raise _fail("contract_info_workspace_mismatch")',
    ):
        _require_text(canary, fact, RUNTIME_CANARY, errors)


def _validate_sdk_dependencies(root: Path, errors: list[str]) -> None:
    """Keep the official MCP client and its direct HTTP client locked offline."""
    project = _load_toml(root / SERVER_PROJECT, errors)
    if isinstance(project, dict):
        project_table = project.get("project")
        dependencies = (
            project_table.get("dependencies") if isinstance(project_table, dict) else None
        )
        if not isinstance(dependencies, list) or dependencies.count(MCP_SDK_REQUIREMENT) != 1:
            errors.append(
                f"{SERVER_PROJECT}: server dependency must pin the stable MCP SDK v1 line "
                f"as {MCP_SDK_REQUIREMENT!r}"
            )
        if not isinstance(dependencies, list) or dependencies.count(HTTPX_REQUIREMENT) != 1:
            errors.append(
                f"{SERVER_PROJECT}: server dependency must declare the canary HTTP client "
                f"as {HTTPX_REQUIREMENT!r}"
            )

    lock = _load_toml(root / WORKSPACE_LOCK, errors)
    if not isinstance(lock, dict):
        return
    packages = lock.get("package")
    if not isinstance(packages, list):
        errors.append(f"{WORKSPACE_LOCK}: package list is required")
        return

    server_rows = [
        package
        for package in packages
        if isinstance(package, dict) and package.get("name") == "omniscience-server"
    ]
    if len(server_rows) != 1:
        errors.append(f"{WORKSPACE_LOCK}: exactly one omniscience-server package is required")
    else:
        server = server_rows[0]
        direct = server.get("dependencies")
        direct_names = (
            [row.get("name") for row in direct if isinstance(row, dict)]
            if isinstance(direct, list)
            else []
        )
        metadata = server.get("metadata")
        requires = metadata.get("requires-dist") if isinstance(metadata, dict) else None
        requirements = (
            [row for row in requires if isinstance(row, dict)]
            if isinstance(requires, list)
            else []
        )
        mcp_rows = [row for row in requirements if row.get("name") == "mcp"]
        httpx_rows = [row for row in requirements if row.get("name") == "httpx"]
        if (
            direct_names.count("mcp") != 1
            or len(mcp_rows) != 1
            or mcp_rows[0].get("specifier") != ">=1.27,<2"
        ):
            errors.append(f"{WORKSPACE_LOCK}: lock metadata must pin the stable MCP SDK v1 line")
        if (
            direct_names.count("httpx") != 1
            or len(httpx_rows) != 1
            or httpx_rows[0].get("specifier") != ">=0.27.0"
        ):
            errors.append(f"{WORKSPACE_LOCK}: lock metadata must pin the canary HTTP client")

    resolved_mcp = [
        package
        for package in packages
        if isinstance(package, dict)
        and package.get("name") == "mcp"
        and isinstance(package.get("version"), str)
    ]
    version = resolved_mcp[0]["version"] if len(resolved_mcp) == 1 else ""
    version_match = re.fullmatch(r"1\.(\d+)\.(\d+)", version)
    if version_match is None or int(version_match.group(1)) < 27:
        errors.append(
            f"{WORKSPACE_LOCK}: resolved MCP SDK must stay on verified v1 at 1.27.0 or newer"
        )


def _require_text(text: str, required: str, surface: Path, errors: list[str]) -> None:
    if required not in text:
        errors.append(f"{surface}: missing required fact {required!r}")


def _validate_docs(root: Path, errors: list[str]) -> None:
    mcp_doc = _read_text(root / MCP_DOC, errors)
    freshness_doc = _read_text(root / FRESHNESS_DOC, errors)
    roadmap = _read_text(root / ROADMAP_DOC, errors)

    for fact in (
        "**Stable contract:** `1.0.0`",
        "**Registry:** `15` tools",
        "**Token profile:** `omniscience-mcp-read-v1`",
        "MCP v0 is superseded",
        "python -m omniscience_server.mcp.materialize",
        "python -m omniscience_server.mcp.canary",
        "--endpoint 'https://omniscience.example/mcp/'",
        "`mcp>=1.27,<2`",
        "`OMNISCIENCE_MCP_MANIFEST_PATH`",
        "`projection_lag_versions`",
        "`fallback.required=true`",
    ):
        _require_text(mcp_doc, fact, MCP_DOC, errors)
    for tool in EXPECTED_TOOLS:
        _require_text(mcp_doc, f"`{tool}`", MCP_DOC, errors)

    for fact in (
        "fresh | stale | unknown | degraded",
        "only sources actually used",
        "missing lineage",
        "direct authoritative sources",
        "projection_lag_versions",
    ):
        _require_text(freshness_doc, fact, FRESHNESS_DOC, errors)

    for fact in (
        "**Current release:** `v0.5.0`",
        "**Current engineering slice:** MCP contract `1.0.0`",
        "production-ha | `ready/queued`",
        "backup-restore | `ready/queued`",
        "read-scaling-priority | `ready/queued`",
        "consumer-severance | `ready/queued`",
        "fail-closed portable canary verifier",
    ):
        _require_text(roadmap, fact, ROADMAP_DOC, errors)


def _frontmatter_value(text: str, key: str) -> str | None:
    match = re.search(rf"(?m)^{re.escape(key)}:\s*['\"]?([^'\"\n]+)['\"]?\s*$", text)
    return match.group(1).strip() if match else None


def _validate_execution_index_entry(
    root: Path, entry: Any, index: int, errors: list[str]
) -> str | None:
    """Structurally validate one execution-index entry.

    Checks field presence/types and that ``task_spec`` resolves to a real
    file, plus (for terminal statuses) that completion evidence has the
    right shape. Deliberately never compares SHAs, PR numbers, or counters —
    those change with every new terminal task and would make this check a
    time bomb. Returns the entry's ``task_id`` when the entry is well-formed,
    otherwise ``None``.
    """
    surface = str(EXECUTION_INDEX)
    if not isinstance(entry, dict):
        errors.append(f"{surface}: entries[{index}] must be an object")
        return None

    well_formed = True
    task_id = entry.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        errors.append(f"{surface}: entries[{index}] missing string task_id")
        well_formed = False

    status = entry.get("status")
    if not isinstance(status, str) or not status:
        errors.append(f"{surface}: entries[{index}] missing string status")
        well_formed = False

    task_spec = entry.get("task_spec")
    if not isinstance(task_spec, str) or not task_spec:
        errors.append(f"{surface}: entries[{index}] missing string task_spec")
        well_formed = False
    elif not task_spec.startswith("docs/specs/") or not (root / task_spec).is_file():
        errors.append(
            f"{surface}: entries[{index}] task_spec does not point to an "
            f"existing file under docs/specs/: {task_spec!r}"
        )
        well_formed = False

    if isinstance(status, str) and status in _TERMINAL_EVIDENCE_STATUSES:
        for field in ("pull_request", "implementation_commit", "merge_commit"):
            if not isinstance(entry.get(field), str) or not entry[field]:
                errors.append(f"{surface}: entries[{index}] missing string {field}")
                well_formed = False

        verification = entry.get("verification")
        if not isinstance(verification, dict):
            errors.append(f"{surface}: entries[{index}] missing verification object")
            well_formed = False
        else:
            provenance = verification.get("provenance")
            if provenance not in _TRUSTED_VERIFICATION_PROVENANCE:
                errors.append(
                    f"{surface}: entries[{index}] verification.provenance "
                    f"must be one of {sorted(_TRUSTED_VERIFICATION_PROVENANCE)}, "
                    f"found {provenance!r}"
                )
                well_formed = False

            check_rollup = verification.get("check_rollup")
            if not isinstance(check_rollup, dict) or not all(
                isinstance(check_rollup.get(key), int) for key in ("success", "skipped", "other")
            ):
                errors.append(
                    f"{surface}: entries[{index}] verification.check_rollup "
                    "must have integer success/skipped/other"
                )
                well_formed = False

            test_evidence = verification.get("test_evidence")
            if not isinstance(test_evidence, dict) or not test_evidence:
                errors.append(f"{surface}: entries[{index}] verification missing test_evidence")
                well_formed = False

    return task_id if well_formed and isinstance(task_id, str) else None


def _validate_governance(root: Path, errors: list[str]) -> None:
    capability_index = _read_text(root / CAPABILITY_INDEX, errors)
    capability = _read_text(root / CAPABILITY_SPEC, errors)
    task_index = _read_text(root / TASK_INDEX, errors)

    _require_text(
        capability_index,
        "[SPEC-MCP](SPEC-MCP-stable-contract-v1.md)",
        CAPABILITY_INDEX,
        errors,
    )
    _require_text(capability_index, "Stable MCP v1", CAPABILITY_INDEX, errors)
    for fact in (
        "Status: ready",
        "human-approved by @100rd on 2026-07-15",
        "genai-enablement ADR-0017",
        "[REQ-MCP-",
        "P-MCP-",
    ):
        _require_text(capability, fact, CAPABILITY_SPEC, errors)

    for task_path, (expected_id, expected_status) in EXPECTED_TASKS.items():
        text = _read_text(root / task_path, errors)
        task_id = _frontmatter_value(text, "id")
        status = _frontmatter_value(text, "status")
        mode = _frontmatter_value(text, "sddMode")
        if task_id != expected_id:
            errors.append(f"{task_path}: expected id {expected_id!r}, found {task_id!r}")
        if status != expected_status:
            errors.append(f"{task_path}: expected status {expected_status!r}, found {status!r}")
        if mode != "full":
            errors.append(f"{task_path}: every issue #350 slice must use Full SDD mode")
        _require_text(
            task_index,
            f"| [{expected_id}]({task_path.name}) | `{expected_status}` |",
            TASK_INDEX,
            errors,
        )

    for fact in (
        "aws-config-phase1.md | `historical-implemented`",
        "discovery-sync-worker.md | `historical-implemented`",
        "gh-issue-355 | `implemented`",
        "execution-index.json",
    ):
        _require_text(task_index, fact, TASK_INDEX, errors)

    evidence = _load_json(root / EXECUTION_INDEX, errors)
    if not isinstance(evidence, dict) or evidence.get("schema_version") != 1:
        errors.append(f"{EXECUTION_INDEX}: schema_version must be 1")
        return
    entries = evidence.get("entries")
    if not isinstance(entries, list) or not entries:
        errors.append(f"{EXECUTION_INDEX}: entries must be a non-empty list")
        return

    task_ids: list[str] = []
    for index, entry in enumerate(entries):
        task_id = _validate_execution_index_entry(root, entry, index, errors)
        if task_id is not None:
            task_ids.append(task_id)
    duplicates = sorted({task_id for task_id in task_ids if task_ids.count(task_id) > 1})
    if duplicates:
        errors.append(f"{EXECUTION_INDEX}: duplicate task_id values: {', '.join(duplicates)}")


def _validate_catalogs(root: Path, errors: list[str]) -> None:
    cline_path = root / ".mcp/cline-marketplace.json"
    pulse_path = root / ".mcp/pulsemcp.json"
    registry_path = root / ".mcp/registry.json"
    cline = _load_json(cline_path, errors)
    pulse = _load_json(pulse_path, errors)
    registry = _load_json(registry_path, errors)

    if isinstance(cline, dict):
        content = cline.get("llmsInstallationContent", "")
        marker = "MCP v1 tools (15): "
        if marker not in content:
            errors.append(f"{cline_path}: missing canonical MCP v1 tool marker")
        else:
            line = content.split(marker, 1)[1].splitlines()[0]
            tools = [tool.strip() for tool in line.rstrip(".").split(",") if tool.strip()]
            compare_tool_surface(
                expected=EXPECTED_TOOLS,
                actual=tools,
                surface=str(cline_path),
                errors=errors,
            )
        _require_text(
            content,
            "/api/v1/tokens/profiles/omniscience-mcp-read-v1",
            Path(".mcp/cline-marketplace.json"),
            errors,
        )

    if isinstance(pulse, dict):
        records = pulse.get("tools", [])
        names = [record.get("name") for record in records if isinstance(record, dict)]
        compare_tool_surface(
            expected=EXPECTED_TOOLS,
            actual=names,
            surface=str(pulse_path),
            errors=errors,
        )

    if isinstance(registry, dict):
        metadata = registry.get("_meta", {}).get("io.github.100rd/omniscience", {})
        validate_contract_version(metadata.get("contractVersion"), str(registry_path), errors)
        names = metadata.get("tools", [])
        if not isinstance(names, list):
            errors.append(f"{registry_path}: tools must be a list")
        else:
            compare_tool_surface(
                expected=EXPECTED_TOOLS,
                actual=names,
                surface=str(registry_path),
                errors=errors,
            )
        remotes = registry.get("remotes")
        canonical_remote = {
            "type": "streamable-http",
            "url": "https://{host}/mcp/",
        }
        if (
            not isinstance(remotes, list)
            or len(remotes) != 1
            or not isinstance(remotes[0], dict)
            or any(remotes[0].get(key) != value for key, value in canonical_remote.items())
        ):
            errors.append(
                f"{registry_path}: remote must publish canonical no-redirect endpoint "
                "https://{host}/mcp/"
            )


def check_repository(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    contract_root = root / CONTRACT_ROOT
    validate_manifest(contract_root, errors)
    registry_names = _validate_registry(contract_root, errors)
    _validate_release_provenance(root, errors)
    _validate_sdk_dependencies(root, errors)
    _validate_contract_info_surface(root, errors)

    runtime_text = _read_text(root / RUNTIME_SERVER, errors)
    if runtime_text:
        try:
            runtime_names = extract_runtime_tool_names(runtime_text)
        except SyntaxError as exc:
            errors.append(f"{RUNTIME_SERVER}: cannot parse runtime registry AST: {exc}")
        else:
            compare_tool_surface(
                expected=EXPECTED_TOOLS,
                actual=runtime_names,
                surface=str(RUNTIME_SERVER),
                errors=errors,
            )
            if registry_names and runtime_names != registry_names:
                errors.append(f"{RUNTIME_SERVER}: runtime and packaged registries differ")

    _validate_docs(root, errors)
    _validate_governance(root, errors)
    _validate_catalogs(root, errors)
    return errors


def main() -> int:
    errors = check_repository()
    if errors:
        print("MCP contract drift check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(
        "MCP contract drift check passed "
        "(offline: runtime, manifest, schemas, SDK lock, docs, SPECs, evidence, catalogs)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
