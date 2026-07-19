"""Qualify one deployed MCP v1 runtime against an immutable release bundle.

The canary intentionally has a small, fail-closed surface.  It reads the
bearer token only from ``OMNISCIENCE_MCP_CANARY_TOKEN``, refuses HTTP
redirects, and emits only stable error codes or a non-secret success receipt.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

CONTRACT_VERSION = "1.0.0"
TOKEN_PROFILE_ID = "omniscience-mcp-read-v1"  # noqa: S105 - public id
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
_MANIFEST_FIELDS = {
    "capabilities",
    "contract_version",
    "schema_set_sha256",
    "schemas",
    "source_commit",
    "token_profile_id",
    "tool_count",
    "tool_descriptions",
    "tool_registry",
}
_CONTRACT_INFO_FIELDS = {
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
_META_FIELDS = {
    "consistency",
    "contract",
    "effective_as_of",
    "fallback",
    "freshness",
    "generated_at",
    "workspace_id",
}
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_MANIFEST_BYTES = 256_000
_MAX_ARTIFACT_BYTES = 1_000_000
_MAX_BUNDLE_FILES = 128
_MAX_TOOL_PAGES = 4
_MAX_RUNTIME_TOOLS = 64
_MAX_TOKEN_BYTES = 4096


class CanaryError(RuntimeError):
    """Stable, non-secret qualification failure."""


class ReleasePin(NamedTuple):
    """Verified immutable values against which a runtime is qualified."""

    bundle_dir: Path
    manifest: dict[str, Any]
    manifest_sha256: str
    source_commit: str
    schema_set_sha256: str
    schema_digests: dict[str, str]
    tool_registry_sha256: str
    tool_names: tuple[str, ...]
    input_schemas: dict[str, dict[str, Any]]
    contract_info_schema_sha256: str


class CanaryReceipt(NamedTuple):
    """Non-secret evidence emitted after exact runtime qualification."""

    status: str
    contract_version: str
    manifest_sha256: str
    source_commit: str
    schema_sha256: str
    tool_registry_sha256: str
    workspace_id: str
    tool_count: int
    generated_at: str


class RuntimeObservation(NamedTuple):
    initialization: Any
    tools: list[Any]
    contract_result: Any


class _DuplicateJSONKeyError(ValueError):
    pass


def _fail(code: str) -> CanaryError:
    return CanaryError(code)


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
        raise _fail("json_value_invalid") from exc


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


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_read(path: Path, *, limit: int, code: str) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size <= 0
                or metadata.st_size > limit
            ):
                raise OSError("artifact size or type is invalid")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                data = handle.read(limit + 1)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise _fail(code) from exc
    if len(data) > limit:
        raise _fail(code)
    return data


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


def _bundle_file_set(bundle_dir: Path) -> set[str]:
    files: set[str] = set()
    try:
        for root, directories, names in os.walk(bundle_dir, followlinks=False):
            root_path = Path(root)
            for name in directories:
                path = root_path / name
                if path.is_symlink() or not path.is_dir():
                    raise _fail("bundle_artifact_invalid")
            for name in names:
                path = root_path / name
                metadata = path.lstat()
                if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                    raise _fail("bundle_artifact_invalid")
                files.add(path.relative_to(bundle_dir).as_posix())
                if len(files) > _MAX_BUNDLE_FILES:
                    raise _fail("bundle_file_set_invalid")
    except CanaryError:
        raise
    except OSError as exc:
        raise _fail("bundle_artifact_invalid") from exc
    return files


def load_release_pin(bundle_dir: Path) -> ReleasePin:
    """Load and independently verify one content-addressed release bundle."""
    if bundle_dir.is_symlink() or not bundle_dir.is_dir():
        raise _fail("bundle_directory_invalid")
    try:
        resolved = bundle_dir.resolve(strict=True)
    except OSError as exc:
        raise _fail("bundle_directory_invalid") from exc
    if resolved.parent.name != "sha256" or not _SHA256.fullmatch(resolved.name):
        raise _fail("bundle_address_invalid")

    manifest_bytes = _safe_read(
        resolved / "manifest.json",
        limit=_MAX_MANIFEST_BYTES,
        code="manifest_invalid",
    )
    manifest_digest = _sha256(manifest_bytes)
    if manifest_digest != resolved.name:
        raise _fail("manifest_address_mismatch")
    manifest = _strict_object(manifest_bytes, code="manifest_invalid")
    if manifest_bytes != _canonical_bytes(manifest):
        raise _fail("manifest_not_canonical")
    if set(manifest) != _MANIFEST_FIELDS:
        raise _fail("manifest_shape_invalid")

    source = manifest.get("source_commit")
    source_commit = source.get("git_commit") if isinstance(source, dict) else None
    if (
        manifest.get("contract_version") != CONTRACT_VERSION
        or manifest.get("token_profile_id") != TOKEN_PROFILE_ID
        or manifest.get("tool_count") != len(EXPECTED_TOOLS)
        or manifest.get("tool_descriptions") != "non-normative"
        or manifest.get("capabilities") != EXPECTED_CAPABILITIES
        or not isinstance(source_commit, str)
        or not _SHA1.fullmatch(source_commit)
        or source_commit == "0" * 40
    ):
        raise _fail("manifest_contract_invalid")

    registry_record = manifest.get("tool_registry")
    if (
        not isinstance(registry_record, dict)
        or set(registry_record) != {"path", "sha256"}
        or registry_record.get("path") != "tool-registry.json"
        or not isinstance(registry_record.get("sha256"), str)
        or not _SHA256.fullmatch(registry_record["sha256"])
    ):
        raise _fail("tool_registry_record_invalid")
    registry_bytes = _safe_read(
        resolved / "tool-registry.json",
        limit=_MAX_ARTIFACT_BYTES,
        code="bundle_artifact_invalid",
    )
    if _sha256(registry_bytes) != registry_record["sha256"]:
        raise _fail("tool_registry_sha256_mismatch")
    registry = _strict_object(registry_bytes, code="tool_registry_invalid")
    if set(registry) != {"contract_version", "description_semantics", "tools"}:
        raise _fail("tool_registry_shape_invalid")
    records = registry.get("tools")
    if not isinstance(records, list):
        raise _fail("tool_registry_invalid")
    names = tuple(record.get("name") if isinstance(record, dict) else None for record in records)
    if (
        registry.get("contract_version") != CONTRACT_VERSION
        or registry.get("description_semantics") != "non-normative"
        or names != EXPECTED_TOOLS
    ):
        raise _fail("tool_registry_invalid")

    schema_records = manifest.get("schemas")
    if not isinstance(schema_records, list):
        raise _fail("schema_index_invalid")
    schema_digests: dict[str, str] = {}
    schemas: dict[str, dict[str, Any]] = {}
    for record in schema_records:
        if (
            not isinstance(record, dict)
            or set(record) != {"path", "sha256"}
            or not isinstance(record.get("sha256"), str)
            or not _SHA256.fullmatch(record["sha256"])
        ):
            raise _fail("schema_record_invalid")
        relative = _schema_path(record.get("path"))
        if relative in schema_digests:
            raise _fail("schema_index_invalid")
        schema_bytes = _safe_read(
            resolved / relative,
            limit=_MAX_ARTIFACT_BYTES,
            code="bundle_artifact_invalid",
        )
        digest = _sha256(schema_bytes)
        if digest != record["sha256"]:
            raise _fail("schema_sha256_mismatch")
        schema_digests[relative] = digest
        schemas[relative] = _strict_object(schema_bytes, code="schema_json_invalid")
    if list(schema_digests) != sorted(schema_digests):
        raise _fail("schema_index_not_sorted")
    schema_rows = "".join(
        f"{path}:{schema_digests[path]}\n" for path in sorted(schema_digests)
    ).encode("utf-8")
    schema_set_sha256 = manifest.get("schema_set_sha256")
    if (
        not isinstance(schema_set_sha256, str)
        or not _SHA256.fullmatch(schema_set_sha256)
        or _sha256(schema_rows) != schema_set_sha256
    ):
        raise _fail("schema_set_sha256_mismatch")

    expected_files = {"manifest.json", "tool-registry.json", *schema_digests}
    if _bundle_file_set(resolved) != expected_files:
        raise _fail("bundle_file_set_invalid")

    input_schemas: dict[str, dict[str, Any]] = {}
    contract_info_schema_sha256: str | None = None
    expected_record_fields = {
        "input_schema",
        "input_schema_sha256",
        "name",
        "required_scope",
        "response_schema",
        "workspace_required",
    }
    for record in records:
        if not isinstance(record, dict) or set(record) != expected_record_fields:
            raise _fail("tool_registry_invalid")
        name = record.get("name")
        input_path = _schema_path(record.get("input_schema"))
        output_path = _schema_path(record.get("response_schema"))
        if (
            not isinstance(name, str)
            or record.get("required_scope") != "search"
            or record.get("workspace_required") is not True
            or input_path not in schemas
            or output_path not in schemas
            or record.get("input_schema_sha256") != schema_digests[input_path]
        ):
            raise _fail("tool_registry_schema_binding_invalid")
        input_schemas[name] = schemas[input_path]
        if name == "contract_info":
            contract_info_schema_sha256 = schema_digests[output_path]
    if contract_info_schema_sha256 is None:
        raise _fail("tool_registry_schema_binding_invalid")

    return ReleasePin(
        bundle_dir=resolved,
        manifest=manifest,
        manifest_sha256=manifest_digest,
        source_commit=source_commit,
        schema_set_sha256=schema_set_sha256,
        schema_digests=schema_digests,
        tool_registry_sha256=registry_record["sha256"],
        tool_names=EXPECTED_TOOLS,
        input_schemas=input_schemas,
        contract_info_schema_sha256=contract_info_schema_sha256,
    )


def _wire_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise _fail("contract_info_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise _fail("contract_info_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise _fail("contract_info_timestamp_invalid")
    return parsed.astimezone(UTC)


def _validate_contract_meta(
    *,
    pin: ReleasePin,
    meta: object,
    expected_workspace_id: uuid.UUID,
) -> str:
    if not isinstance(meta, dict) or set(meta) != _META_FIELDS:
        raise _fail("contract_info_meta_shape_mismatch")
    contract = meta.get("contract")
    if contract != {
        "version": CONTRACT_VERSION,
        "commit": pin.source_commit,
        "schema_sha256": pin.contract_info_schema_sha256,
    }:
        raise _fail("contract_info_meta_contract_mismatch")
    if meta.get("workspace_id") != str(expected_workspace_id):
        raise _fail("contract_info_workspace_mismatch")

    generated_at = meta.get("generated_at")
    if _wire_timestamp(generated_at) != _wire_timestamp(meta.get("effective_as_of")):
        raise _fail("contract_info_timestamp_mismatch")
    freshness = meta.get("freshness")
    if not isinstance(freshness, dict) or freshness != {
        "status": "unknown",
        "evaluated_at": generated_at,
        "oldest_source_age_seconds": None,
        "stale_source_ids": [],
        "reason": "source_free_contract_metadata",
    }:
        raise _fail("contract_info_freshness_mismatch")
    if meta.get("consistency") != {
        "status": "unknown",
        "degraded_subsystems": [],
        "projection_lag_versions": 0,
    }:
        raise _fail("contract_info_consistency_mismatch")
    fallback = meta.get("fallback")
    if not isinstance(fallback, dict) or fallback.get("required") is not False:
        raise _fail("contract_info_fallback_required")
    if fallback != {"required": False, "reason": None}:
        raise _fail("contract_info_fallback_mismatch")
    assert isinstance(generated_at, str)  # noqa: S101 - validated above
    return generated_at


def _structured_contract_payload(contract_result: Any) -> dict[str, Any]:
    if getattr(contract_result, "isError", False):
        raise _fail("contract_info_call_failed")
    payload = getattr(contract_result, "structuredContent", None)
    if not isinstance(payload, dict):
        raise _fail("contract_info_structured_content_missing")
    content = getattr(contract_result, "content", None)
    if content:
        if (
            not isinstance(content, list)
            or len(content) != 1
            or getattr(content[0], "type", None) != "text"
            or not isinstance(getattr(content[0], "text", None), str)
        ):
            raise _fail("contract_info_content_mismatch")
        text_payload = _strict_object(
            content[0].text.encode("utf-8"),
            code="contract_info_content_mismatch",
        )
        if text_payload != payload:
            raise _fail("contract_info_content_mismatch")
    return payload


def verify_runtime_observation(
    *,
    pin: ReleasePin,
    initialization: Any,
    tools: Sequence[Any],
    contract_result: Any,
    expected_workspace_id: uuid.UUID,
) -> CanaryReceipt:
    """Verify MCP initialization, tool schemas, and authenticated handshake."""
    capabilities = getattr(initialization, "capabilities", None)
    if capabilities is None or getattr(capabilities, "tools", None) is None:
        raise _fail("runtime_tools_capability_missing")
    names = [getattr(tool, "name", None) for tool in tools]
    if (
        any(not isinstance(name, str) for name in names)
        or len(names) > _MAX_RUNTIME_TOOLS
        or len(names) != len(set(names))
        or tuple(sorted(str(name) for name in names)) != pin.tool_names
    ):
        raise _fail("runtime_tool_surface_mismatch")
    for tool in tools:
        name = getattr(tool, "name", None)
        input_schema = getattr(tool, "inputSchema", None)
        if (
            not isinstance(name, str)
            or not isinstance(input_schema, dict)
            or input_schema != pin.input_schemas[name]
        ):
            raise _fail("runtime_input_schema_mismatch")

    payload = _structured_contract_payload(contract_result)
    if set(payload) != _CONTRACT_INFO_FIELDS:
        raise _fail("contract_info_shape_mismatch")
    if payload.get("manifest") != pin.manifest:
        raise _fail("contract_info_manifest_mismatch")
    expected_values = {
        "version": CONTRACT_VERSION,
        "commit": pin.source_commit,
        "manifest_sha256": pin.manifest_sha256,
        "schema_sha256": pin.schema_set_sha256,
        "schema_digests": pin.schema_digests,
        "tool_registry_sha256": pin.tool_registry_sha256,
        "token_profile_id": TOKEN_PROFILE_ID,
        "capabilities": EXPECTED_CAPABILITIES,
    }
    for field, expected in expected_values.items():
        if payload.get(field) != expected:
            raise _fail(f"contract_info_{field}_mismatch")
    generated_at = _validate_contract_meta(
        pin=pin,
        meta=payload.get("meta"),
        expected_workspace_id=expected_workspace_id,
    )
    return CanaryReceipt(
        status="passed",
        contract_version=CONTRACT_VERSION,
        manifest_sha256=pin.manifest_sha256,
        source_commit=pin.source_commit,
        schema_sha256=pin.schema_set_sha256,
        tool_registry_sha256=pin.tool_registry_sha256,
        workspace_id=str(expected_workspace_id),
        tool_count=len(pin.tool_names),
        generated_at=generated_at,
    )


def validate_endpoint(endpoint: str) -> str:
    """Require a canonical no-redirect HTTPS endpoint (or numeric loopback)."""
    if (
        not isinstance(endpoint, str)
        or len(endpoint) > 2048
        or any(character.isspace() or ord(character) < 0x20 for character in endpoint)
        or "\x7f" in endpoint
    ):
        raise _fail("endpoint_invalid")
    parsed = urlsplit(endpoint)
    loopback = parsed.hostname in {"127.0.0.1", "::1"}
    if (
        parsed.scheme not in {"http", "https"}
        or (parsed.scheme != "https" and not loopback)
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or not parsed.path.endswith("/")
        or "//" in parsed.path
        or ".." in PurePosixPath(parsed.path).parts
    ):
        raise _fail("endpoint_invalid")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise _fail("endpoint_invalid") from exc
    return endpoint


def validate_token(token: str) -> str:
    """Reject missing, oversized, or header-unsafe bearer values."""
    if (
        not isinstance(token, str)
        or not token
        or len(token.encode("utf-8")) > _MAX_TOKEN_BYTES
        or any(character.isspace() or ord(character) < 0x20 for character in token)
        or "\x7f" in token
    ):
        raise _fail("token_invalid")
    return token


async def _observe_with_client(
    *, endpoint: str, http_client: httpx.AsyncClient
) -> RuntimeObservation:
    async with (
        streamable_http_client(
            endpoint,
            http_client=http_client,
            terminate_on_close=True,
        ) as (read_stream, write_stream, _get_session_id),
        ClientSession(
            read_stream,
            write_stream,
            read_timeout_seconds=timedelta(seconds=15),
        ) as session,
    ):
        initialization = await session.initialize()
        tools: list[Any] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(_MAX_TOOL_PAGES):
            page = await session.list_tools(cursor=cursor)
            tools.extend(page.tools)
            if len(tools) > _MAX_RUNTIME_TOOLS:
                raise _fail("runtime_tool_surface_unbounded")
            cursor = page.nextCursor
            if cursor is None:
                break
            if cursor in seen_cursors:
                raise _fail("runtime_tool_pagination_invalid")
            seen_cursors.add(cursor)
        else:
            raise _fail("runtime_tool_pagination_unbounded")
        contract_result = await session.call_tool("contract_info", {})
    return RuntimeObservation(initialization, tools, contract_result)


async def _observe_streamable_http(*, endpoint: str, token: str) -> RuntimeObservation:
    headers = {"Authorization": f"Bearer {token}"}
    timeout = httpx.Timeout(15.0, connect=5.0)
    async with httpx.AsyncClient(
        headers=headers,
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
    ) as http_client:
        async with asyncio.timeout(30):
            return await _observe_with_client(endpoint=endpoint, http_client=http_client)


async def run_canary(
    *,
    bundle_dir: Path,
    endpoint: str,
    token: str,
    expected_workspace_id: uuid.UUID,
) -> CanaryReceipt:
    """Run the official Streamable HTTP client lifecycle and qualify it."""
    pin = load_release_pin(bundle_dir)
    endpoint = validate_endpoint(endpoint)
    token = validate_token(token)
    try:
        observation = await _observe_streamable_http(endpoint=endpoint, token=token)
    except CanaryError:
        raise
    except Exception as exc:
        raise _fail("transport_failed") from exc
    return verify_runtime_observation(
        pin=pin,
        initialization=observation.initialization,
        tools=observation.tools,
        contract_result=observation.contract_result,
        expected_workspace_id=expected_workspace_id,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--workspace-id")
    args = parser.parse_args(argv)
    token = os.environ.get("OMNISCIENCE_MCP_CANARY_TOKEN", "")
    workspace_value = args.workspace_id or os.environ.get(
        "OMNISCIENCE_MCP_CANARY_WORKSPACE_ID", ""
    )
    try:
        try:
            workspace_id = uuid.UUID(workspace_value)
        except (ValueError, AttributeError) as exc:
            raise _fail("workspace_id_invalid") from exc
        receipt = asyncio.run(
            run_canary(
                bundle_dir=args.bundle_dir,
                endpoint=args.endpoint,
                token=token,
                expected_workspace_id=workspace_id,
            )
        )
    except CanaryError as exc:
        sys.stderr.write(f"MCP v1 canary failed: {exc}\n")
        return 1
    sys.stdout.write(json.dumps(receipt._asdict(), sort_keys=True, separators=(",", ":")) + "\n")
    return 0


__all__ = [
    "CanaryError",
    "CanaryReceipt",
    "ReleasePin",
    "load_release_pin",
    "main",
    "run_canary",
    "validate_endpoint",
    "validate_token",
    "verify_runtime_observation",
]


if __name__ == "__main__":
    raise SystemExit(main())
