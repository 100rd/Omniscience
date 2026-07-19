"""Stable, content-addressed Omniscience MCP contract v1."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any

CONTRACT_VERSION = "1.0.0"
TOKEN_PROFILE_ID = "omniscience-mcp-read-v1"  # noqa: S105 - public profile identifier
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DEVELOPMENT_COMMIT = "0" * 40
_MAX_MANIFEST_BYTES = 256_000


class _DuplicateJSONKeyError(ValueError):
    pass


def contract_root() -> Path:
    """Return the installed v1 contract resource directory."""
    return Path(__file__).with_name("contracts") / "v1"


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid_contract_artifact:{path.name}")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
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


def _strict_json_object(data: bytes) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateJSONKeyError(key)
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    value = json.loads(
        data.decode("utf-8"),
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("manifest must be an object")
    return value


def _release_build_enabled() -> bool:
    return os.environ.get("OMNISCIENCE_RELEASE_BUILD", "").lower() in {
        "1",
        "true",
        "yes",
    }


def _release_manifest_snapshot() -> tuple[dict[str, Any], bytes]:
    """Read and verify the exact immutable manifest used by a release runtime."""
    path_value = os.environ.get("OMNISCIENCE_MCP_MANIFEST_PATH", "")
    configured_commit = os.environ.get("OMNISCIENCE_MCP_SOURCE_COMMIT", "").lower()
    if (
        not path_value
        or len(path_value) > 4096
        or "\x00" in path_value
        or not _SHA_RE.fullmatch(configured_commit)
        or configured_commit == _DEVELOPMENT_COMMIT
    ):
        raise RuntimeError("contract_provenance_unavailable")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path_value, flags)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size <= 0
                or metadata.st_size > _MAX_MANIFEST_BYTES
            ):
                raise ValueError("invalid release manifest file")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                data = handle.read(_MAX_MANIFEST_BYTES + 1)
        finally:
            os.close(descriptor)
        manifest = _strict_json_object(data)
        if data != _canonical_json_bytes(manifest):
            raise ValueError("release manifest is not canonical")
        source_commit = manifest.get("source_commit")
        if source_commit != {"git_commit": configured_commit}:
            raise ValueError("release manifest commit mismatch")
        expected = load_manifest()
        expected["source_commit"] = {"git_commit": configured_commit}
        if manifest != expected:
            raise ValueError("release manifest differs from packaged contract")
        if verify_contract_artifacts():
            raise ValueError("packaged contract artifacts differ from manifest")
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise RuntimeError("contract_provenance_unavailable") from exc
    return manifest, data


def load_manifest() -> dict[str, Any]:
    """Load the committed v1 manifest."""
    return _load_json(contract_root() / "manifest.json")


def load_tool_registry() -> dict[str, Any]:
    """Load the committed canonical tool registry."""
    manifest = load_manifest()
    return _load_json(contract_root() / manifest["tool_registry"]["path"])


def effective_manifest(source_commit: str | None = None) -> dict[str, Any]:
    """Materialize the manifest template with concrete release provenance."""
    commit = source_commit or resolve_source_commit()
    manifest = load_manifest()
    manifest["source_commit"] = {"git_commit": commit}
    return manifest


def effective_manifest_bytes(source_commit: str | None = None) -> bytes:
    """Return canonical content-addressed bytes for the effective manifest."""
    return _canonical_json_bytes(effective_manifest(source_commit))


def manifest_sha256(source_commit: str | None = None) -> str:
    """Return the digest consumers pin for the effective manifest bytes."""
    if source_commit is None and _release_build_enabled():
        _, manifest_bytes = _release_manifest_snapshot()
        return hashlib.sha256(manifest_bytes).hexdigest()
    return hashlib.sha256(effective_manifest_bytes(source_commit)).hexdigest()


def tool_schema_sha256(tool_name: str) -> str:
    """Return the digest of the exact output schema pinned for one tool."""
    registry = load_tool_registry()
    try:
        schema_path = next(
            entry["response_schema"] for entry in registry["tools"] if entry["name"] == tool_name
        )
    except StopIteration as exc:
        raise KeyError(f"unknown_mcp_tool:{tool_name}") from exc
    manifest = load_manifest()
    try:
        return str(
            next(
                record["sha256"] for record in manifest["schemas"] if record["path"] == schema_path
            )
        )
    except StopIteration as exc:
        raise RuntimeError(f"unmanifested_tool_schema:{tool_name}") from exc


def verify_contract_artifacts() -> list[str]:
    """Return deterministic artifact skew errors without network access."""
    manifest = load_manifest()
    errors: list[str] = []
    registry_record = manifest["tool_registry"]
    registry_path = contract_root() / registry_record["path"]
    if _sha256(registry_path) != registry_record["sha256"]:
        errors.append("tool_registry_sha256_mismatch")

    schema_rows: list[str] = []
    for record in sorted(manifest["schemas"], key=lambda item: item["path"]):
        path = contract_root() / record["path"]
        digest = _sha256(path)
        if digest != record["sha256"]:
            errors.append(f"schema_sha256_mismatch:{record['path']}")
        schema_rows.append(f"{record['path']}:{digest}")
    aggregate = hashlib.sha256(("\n".join(schema_rows) + "\n").encode()).hexdigest()
    if aggregate != manifest["schema_set_sha256"]:
        errors.append("schema_set_sha256_mismatch")

    registry = load_tool_registry()
    names = [entry["name"] for entry in registry.get("tools", [])]
    manifested = {record["path"]: record["sha256"] for record in manifest["schemas"]}
    for entry in registry.get("tools", []):
        input_path = entry.get("input_schema")
        input_digest = entry.get("input_schema_sha256")
        if not isinstance(input_path, str) or not isinstance(input_digest, str):
            errors.append(f"input_schema_missing:{entry.get('name')}")
            continue
        actual = _sha256(contract_root() / input_path)
        if actual != input_digest or manifested.get(input_path) != input_digest:
            errors.append(f"input_schema_sha256_mismatch:{entry['name']}")
    if names != sorted(names):
        errors.append("tool_registry_not_sorted")
    if len(names) != manifest["tool_count"]:
        errors.append("tool_count_mismatch")
    if manifest["contract_version"] != CONTRACT_VERSION:
        errors.append("contract_version_mismatch")
    return errors


def resolve_source_commit() -> str:
    """Resolve release provenance without making the manifest self-referential.

    Release builds inject ``OMNISCIENCE_MCP_SOURCE_COMMIT``.  A release build
    fails closed when it is absent or malformed.  Local development uses an
    explicit all-zero sentinel so responses remain schema-valid while clearly
    not claiming release provenance.
    """
    if _release_build_enabled():
        manifest, _ = _release_manifest_snapshot()
        return str(manifest["source_commit"]["git_commit"])
    commit = os.environ.get("OMNISCIENCE_MCP_SOURCE_COMMIT", "").lower()
    if _SHA_RE.fullmatch(commit):
        return commit
    return _DEVELOPMENT_COMMIT


def is_pinnable_source_commit(commit: str) -> bool:
    """Return whether *commit* is a concrete release identity consumers can pin."""
    return _SHA_RE.fullmatch(commit) is not None and commit != _DEVELOPMENT_COMMIT


def build_contract_info(workspace_id: uuid.UUID) -> dict[str, Any]:
    """Build the authenticated, source-free contract discovery payload."""
    from omniscience_server.mcp.metadata import build_source_free_meta

    if _release_build_enabled():
        materialized_manifest, materialized_bytes = _release_manifest_snapshot()
        commit = str(materialized_manifest["source_commit"]["git_commit"])
    else:
        commit = resolve_source_commit()
        materialized_manifest = effective_manifest(commit)
        materialized_bytes = _canonical_json_bytes(materialized_manifest)
    return {
        "version": CONTRACT_VERSION,
        "commit": commit,
        "manifest_sha256": hashlib.sha256(materialized_bytes).hexdigest(),
        "manifest": materialized_manifest,
        "schema_sha256": materialized_manifest["schema_set_sha256"],
        "schema_digests": {
            record["path"]: record["sha256"]
            for record in sorted(materialized_manifest["schemas"], key=lambda item: item["path"])
        },
        "tool_registry_sha256": materialized_manifest["tool_registry"]["sha256"],
        "token_profile_id": TOKEN_PROFILE_ID,
        "capabilities": materialized_manifest["capabilities"],
        "meta": build_source_free_meta(
            workspace_id=workspace_id,
            source_commit=commit,
            reason="source_free_contract_metadata",
        ),
    }


__all__ = [
    "CONTRACT_VERSION",
    "TOKEN_PROFILE_ID",
    "build_contract_info",
    "contract_root",
    "effective_manifest",
    "effective_manifest_bytes",
    "is_pinnable_source_commit",
    "load_manifest",
    "load_tool_registry",
    "manifest_sha256",
    "resolve_source_commit",
    "tool_schema_sha256",
    "verify_contract_artifacts",
]
