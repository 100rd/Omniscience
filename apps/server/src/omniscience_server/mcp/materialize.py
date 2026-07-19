"""Materialize an immutable MCP v1 release bundle from one exact Git commit."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple

CONTRACT_ROOT = PurePosixPath("apps/server/src/omniscience_server/mcp/contracts/v1")
EXPECTED_TOOLS = (
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
)
EXPECTED_CAPABILITIES = {
    "auth": [
        "workspace-bound",
        "exact-search-scope",
        "bounded-expiry",
        "rotation",
        "revocation",
    ],
    "consistency": ["converged", "degraded", "unknown"],
    "freshness": ["fresh", "stale", "unknown", "degraded"],
    "severance": ["direct-source-fallback"],
}
EXPECTED_SOURCE_DESCRIPTOR = {
    "environment_variable": "OMNISCIENCE_MCP_SOURCE_COMMIT",
    "release_required": True,
    "strategy": "runtime-injection",
}
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class MaterializationError(RuntimeError):
    """Stable fail-closed release materialization failure."""


class MaterializedBundle(NamedTuple):
    bundle_dir: Path
    manifest_sha256: str
    source_commit: str


class _DuplicateJSONKeyError(ValueError):
    pass


def _fail(code: str) -> MaterializationError:
    return MaterializationError(code)


def _git(repo: Path, *args: str, binary: bool = False) -> str | bytes:
    git = shutil.which("git")
    if git is None:
        raise _fail("source_repository_invalid")
    try:
        result = subprocess.run(  # noqa: S603 - fixed executable; args are internal/validated
            [git, *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=not binary,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise _fail("source_repository_invalid") from exc
    stdout: object = result.stdout
    if binary:
        if not isinstance(stdout, bytes):
            raise _fail("source_repository_invalid")
        return stdout
    if not isinstance(stdout, str):
        raise _fail("source_repository_invalid")
    return stdout


def _strict_object(data: bytes, *, code: str) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateJSONKeyError(key)
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite constant: {value}")

    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise _fail(code) from exc
    if not isinstance(value, dict):
        raise _fail(code)
    return value


def _canonical_bytes(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise _fail("manifest_not_canonicalizable") from exc


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _commit_bytes(repo: Path, source_commit: str, relative_path: str) -> bytes:
    value = _git(repo, "show", f"{source_commit}:{relative_path}", binary=True)
    if not isinstance(value, bytes):
        raise _fail("source_repository_invalid")
    return value


def _schema_path(value: object) -> str:
    if not isinstance(value, str):
        raise _fail("schema_path_invalid")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or len(path.parts) != 2
        or path.parts[0] != "schemas"
        or path.parts[1] in {"", ".", ".."}
        or path.suffix != ".json"
    ):
        raise _fail("schema_path_invalid")
    return value


def _validate_source(repo: Path, source_commit: str) -> None:
    if not _SHA1.fullmatch(source_commit) or source_commit == "0" * 40:
        raise _fail("source_commit_invalid")
    head = str(_git(repo, "rev-parse", "HEAD")).strip().lower()
    if head != source_commit:
        raise _fail("source_commit_not_head")
    status = str(
        _git(
            repo,
            "-c",
            "core.quotepath=false",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
    )
    if status:
        raise _fail("source_worktree_not_clean")


def _validated_contract_artifacts(
    repo: Path, source_commit: str
) -> tuple[dict[str, Any], dict[str, bytes]]:
    root = CONTRACT_ROOT.as_posix()
    template_bytes = _commit_bytes(repo, source_commit, f"{root}/manifest.json")
    template = _strict_object(template_bytes, code="manifest_invalid")
    if set(template) != {
        "capabilities",
        "contract_version",
        "schema_set_sha256",
        "schemas",
        "source_commit",
        "token_profile_id",
        "tool_count",
        "tool_descriptions",
        "tool_registry",
    }:
        raise _fail("manifest_shape_invalid")
    if (
        template.get("contract_version") != "1.0.0"
        or template.get("source_commit") != EXPECTED_SOURCE_DESCRIPTOR
        or template.get("token_profile_id") != "omniscience-mcp-read-v1"
        or template.get("tool_count") != len(EXPECTED_TOOLS)
        or template.get("tool_descriptions") != "non-normative"
        or template.get("capabilities") != EXPECTED_CAPABILITIES
    ):
        raise _fail("manifest_contract_invalid")

    registry_record = template.get("tool_registry")
    if (
        not isinstance(registry_record, dict)
        or set(registry_record) != {"path", "sha256"}
        or registry_record.get("path") != "tool-registry.json"
        or not isinstance(registry_record.get("sha256"), str)
        or not _SHA256.fullmatch(registry_record["sha256"])
    ):
        raise _fail("tool_registry_record_invalid")
    registry_bytes = _commit_bytes(repo, source_commit, f"{root}/tool-registry.json")
    if _sha256(registry_bytes) != registry_record["sha256"]:
        raise _fail("tool_registry_sha256_mismatch")
    registry = _strict_object(registry_bytes, code="tool_registry_invalid")
    if set(registry) != {"contract_version", "description_semantics", "tools"}:
        raise _fail("tool_registry_shape_invalid")
    tools = registry.get("tools")
    if not isinstance(tools, list):
        raise _fail("tool_registry_invalid")
    names = tuple(record.get("name") if isinstance(record, dict) else None for record in tools)
    if (
        registry.get("contract_version") != "1.0.0"
        or registry.get("description_semantics") != "non-normative"
        or names != EXPECTED_TOOLS
    ):
        raise _fail("tool_registry_invalid")

    schema_records = template.get("schemas")
    if not isinstance(schema_records, list):
        raise _fail("schema_index_invalid")
    artifacts: dict[str, bytes] = {"tool-registry.json": registry_bytes}
    schema_digests: dict[str, str] = {}
    for record in schema_records:
        if (
            not isinstance(record, dict)
            or set(record) != {"path", "sha256"}
            or not isinstance(record.get("sha256"), str)
            or not _SHA256.fullmatch(record["sha256"])
        ):
            raise _fail("schema_record_invalid")
        relative_path = _schema_path(record.get("path"))
        if relative_path in schema_digests:
            raise _fail("schema_index_invalid")
        schema_bytes = _commit_bytes(repo, source_commit, f"{root}/{relative_path}")
        actual_digest = _sha256(schema_bytes)
        if actual_digest != record["sha256"]:
            raise _fail("schema_sha256_mismatch")
        schema_digests[relative_path] = actual_digest
        artifacts[relative_path] = schema_bytes
    if list(schema_digests) != sorted(schema_digests):
        raise _fail("schema_index_not_sorted")

    listed = str(
        _git(
            repo,
            "ls-tree",
            "-r",
            "--name-only",
            source_commit,
            "--",
            f"{root}/schemas",
        )
    ).splitlines()
    committed_schema_paths = sorted(
        str(PurePosixPath(path).relative_to(CONTRACT_ROOT)) for path in listed
    )
    if committed_schema_paths != sorted(schema_digests):
        raise _fail("schema_index_incomplete")

    schema_rows = "".join(
        f"{path}:{schema_digests[path]}\n" for path in sorted(schema_digests)
    ).encode("utf-8")
    if template.get("schema_set_sha256") != _sha256(schema_rows):
        raise _fail("schema_set_sha256_mismatch")

    for record in tools:
        if not isinstance(record, dict) or set(record) != {
            "input_schema",
            "input_schema_sha256",
            "name",
            "required_scope",
            "response_schema",
            "workspace_required",
        }:
            raise _fail("tool_registry_invalid")
        input_path = _schema_path(record.get("input_schema"))
        response_path = _schema_path(record.get("response_schema"))
        if (
            record.get("required_scope") != "search"
            or record.get("workspace_required") is not True
            or input_path not in schema_digests
            or response_path not in schema_digests
            or record.get("input_schema_sha256") != schema_digests[input_path]
        ):
            raise _fail("tool_registry_schema_binding_invalid")

    materialized = dict(template)
    materialized["source_commit"] = {"git_commit": source_commit}
    return materialized, artifacts


def _verify_existing_bundle(target: Path, files: dict[str, bytes]) -> bool:
    if not target.is_dir() or target.is_symlink():
        return False
    disk_paths: list[str] = []
    for path in target.rglob("*"):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            return False
        if path.is_file():
            disk_paths.append(path.relative_to(target).as_posix())
    if sorted(disk_paths) != sorted(files):
        return False
    return all((target / relative).read_bytes() == content for relative, content in files.items())


def _publish_bundle(target: Path, files: dict[str, bytes]) -> None:
    sha_root = target.parent
    sha_root.mkdir(parents=True, exist_ok=True)
    if sha_root.is_symlink():
        raise _fail("output_root_invalid")
    if target.exists():
        if _verify_existing_bundle(target, files):
            return
        raise _fail("content_address_conflict")

    temporary = sha_root / f".{target.name}.tmp-{uuid.uuid4().hex}"
    try:
        temporary.mkdir(mode=0o755)
        for relative, content in sorted(files.items()):
            output = temporary / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        temporary.rename(target)
    except FileExistsError:
        if not _verify_existing_bundle(target, files):
            raise _fail("content_address_conflict") from None
    except OSError as exc:
        raise _fail("bundle_publish_failed") from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def materialize_release_bundle(
    *, repo: Path, source_commit: str, output_root: Path
) -> MaterializedBundle:
    """Build one verified content-addressed bundle from clean exact ``HEAD``."""
    repo = repo.resolve()
    output_root = output_root.resolve()
    if not repo.is_dir() or repo.is_symlink():
        raise _fail("source_repository_invalid")
    source_commit = source_commit.lower()
    _validate_source(repo, source_commit)
    manifest, artifacts = _validated_contract_artifacts(repo, source_commit)
    manifest_bytes = _canonical_bytes(manifest)
    manifest_digest = _sha256(manifest_bytes)
    files = {"manifest.json": manifest_bytes, **artifacts}
    bundle_dir = output_root / "sha256" / manifest_digest
    _publish_bundle(bundle_dir, files)
    if not _verify_existing_bundle(bundle_dir, files):
        raise _fail("bundle_readback_mismatch")
    return MaterializedBundle(
        bundle_dir=bundle_dir,
        manifest_sha256=manifest_digest,
        source_commit=source_commit,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args(argv)
    output_root = args.output_root or args.repo / "build" / "mcp-v1"
    try:
        result = materialize_release_bundle(
            repo=args.repo,
            source_commit=args.source_commit,
            output_root=output_root,
        )
    except MaterializationError as exc:
        sys.stderr.write(f"MCP v1 materialization failed: {exc}\n")
        return 1
    sys.stdout.write(
        json.dumps(
            {
                "bundle_dir": str(result.bundle_dir),
                "manifest_sha256": result.manifest_sha256,
                "source_commit": result.source_commit,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
