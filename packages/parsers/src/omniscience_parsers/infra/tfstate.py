"""Terraform state file parser (JSON format v4).

Each resource instance in the state file becomes a :class:`~omniscience_parsers.base.Section` with:
- ``heading_path``: ``[module_address, resource_type, resource_name]``
- ``symbol``: the full resource address, e.g. ``"aws_s3_bucket.logs"`` or
  ``"module.vpc.aws_subnet.private[0]"``
- ``text``: JSON-formatted attributes of the instance
- ``metadata``: ``{"arn": ..., "id": ..., "region": ..., "account": ...,
  "provider": ..., "mode": ...}``

Only format version 4 is supported.  Unknown versions produce an empty document
with a ``parse_error`` entry in the document-level metadata so callers can log
gracefully without raising.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from omniscience_parsers.base import ParsedDocument, Section

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SUPPORTED_VERSION = 4

_TFSTATE_CONTENT_TYPE = "application/x-tfstate"

# Attribute keys that commonly hold ARN-like values.  We pick the first
# non-empty one found.
_ARN_KEYS = ("arn", "bucket_arn", "role_arn", "function_arn", "topic_arn", "queue_arn")

# Attribute keys that carry region information.
_REGION_KEYS = ("region", "availability_zone")

# Attribute keys that carry AWS account id.
_ACCOUNT_KEYS = ("account_id", "owner_id")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _pick(attrs: dict[str, Any], keys: tuple[str, ...]) -> str:
    """Return the first non-empty string value found among *keys* in *attrs*."""
    for k in keys:
        raw = attrs.get(k)
        if raw and isinstance(raw, str):
            val: str = raw
            return val
    return ""


def _normalise_module(module_address: str) -> str:
    """Return a clean module address string (empty string for root module)."""
    # Terraform uses "module.foo" but the root module address is "" or "root".
    addr = module_address.strip()
    if addr in ("", "root"):
        return ""
    return addr


def _resource_address(
    module_address: str,
    resource_mode: str,
    resource_type: str,
    resource_name: str,
    instance_index: str | int | None,
) -> str:
    """Build a canonical resource address matching ``terraform show`` output."""
    # data sources have "data." prefix in their address
    if resource_mode == "data":
        type_and_name = f"data.{resource_type}.{resource_name}"
    else:
        type_and_name = f"{resource_type}.{resource_name}"

    # Add index suffix for for_each / count resources
    if instance_index is not None and instance_index != "":
        if isinstance(instance_index, str):
            type_and_name = f'{type_and_name}["{instance_index}"]'
        else:
            type_and_name = f"{type_and_name}[{instance_index}]"

    if module_address:
        return f"{module_address}.{type_and_name}"
    return type_and_name


def _parse_v4(data: dict[str, Any], file_path: str) -> list[Section]:
    """Extract sections from a v4-format state document."""
    sections: list[Section] = []
    resources: list[dict[str, Any]] = data.get("resources", [])

    line_cursor = 1  # approximate — state files have no meaningful line structure

    for resource in resources:
        if not isinstance(resource, dict):
            continue

        resource_mode: str = resource.get("mode", "managed")
        resource_type: str = resource.get("type", "")
        resource_name: str = resource.get("name", "")
        module_address: str = _normalise_module(resource.get("module", ""))
        provider_config: str = resource.get("provider", "")

        if not resource_type or not resource_name:
            log.debug(
                "tfstate_skip_unnamed_resource",
                file_path=file_path,
                resource=resource,
            )
            continue

        instances: list[dict[str, Any]] = resource.get("instances", [])

        for instance in instances:
            if not isinstance(instance, dict):
                continue

            index_key = instance.get("index_key")  # None, int, or str
            attrs: dict[str, Any] = instance.get("attributes", {}) or {}

            address = _resource_address(
                module_address,
                resource_mode,
                resource_type,
                resource_name,
                index_key,
            )

            # Build heading path: module (if any) → resource_type → resource_name
            heading: list[str] = []
            if module_address:
                heading.append(module_address)
            heading.extend([resource_type, resource_name])

            # Serialise attributes as the section text
            text = json.dumps(attrs, indent=2, default=str)
            line_count = max(1, text.count("\n") + 1)

            # Extract well-known metadata fields
            arn = _pick(attrs, _ARN_KEYS)
            resource_id = str(attrs.get("id", "")) if attrs.get("id") is not None else ""
            region = _pick(attrs, _REGION_KEYS)
            account = _pick(attrs, _ACCOUNT_KEYS)

            meta: dict[str, Any] = {
                "arn": arn,
                "id": resource_id,
                "region": region,
                "account": account,
                "provider": provider_config,
                "mode": resource_mode,
                "resource_type": resource_type,
                "resource_name": resource_name,
            }

            sections.append(
                Section(
                    heading_path=heading,
                    text=text,
                    line_start=line_cursor,
                    line_end=line_cursor + line_count - 1,
                    symbol=address,
                    metadata=meta,
                )
            )
            line_cursor += line_count

    return sections


# ---------------------------------------------------------------------------
# TfStateParser
# ---------------------------------------------------------------------------


class TfStateParser:
    """Parses Terraform state files (JSON format v4).

    Each resource instance in the state file becomes a
    :class:`~omniscience_parsers.base.Section` with:

    - ``symbol``: the resource address, e.g. ``"aws_s3_bucket.logs"``
    - ``heading_path``: ``[resource_type, resource_name]`` (or with module prefix)
    - ``text``: JSON-formatted instance attributes
    - ``metadata``: ``{"arn": ..., "id": ..., "region": ..., "account": ...,
      "provider": ..., "mode": ...}``

    Only state format version 4 is supported.  Files with other versions
    produce an empty document with a ``parse_error`` key in metadata.
    """

    connector_type = "tfstate"

    def can_handle(self, content_type: str, file_extension: str) -> bool:
        """Return ``True`` for ``.tfstate`` files or the ``application/x-tfstate`` content type."""
        ext = file_extension.lower()
        if ext == ".tfstate":
            return True
        return content_type == _TFSTATE_CONTENT_TYPE

    def parse(self, content: bytes, file_path: str = "") -> ParsedDocument:
        """Parse *content* as a Terraform state file.

        Returns a :class:`ParsedDocument` with one :class:`Section` per
        resource instance.  If the file cannot be parsed (invalid JSON, wrong
        format version, etc.) the document will have an empty ``sections`` list
        and a ``parse_error`` entry in ``metadata``.
        """
        try:
            data: dict[str, Any] = json.loads(content.decode(errors="replace"))
        except json.JSONDecodeError as exc:
            log.warning("tfstate_json_parse_error", file_path=file_path, error=str(exc))
            return ParsedDocument(
                sections=[],
                content_type=_TFSTATE_CONTENT_TYPE,
                language="json",
                metadata={"parse_error": f"JSON decode error: {exc}"},
            )

        if not isinstance(data, dict):
            return ParsedDocument(
                sections=[],
                content_type=_TFSTATE_CONTENT_TYPE,
                language="json",
                metadata={"parse_error": "State file root is not a JSON object"},
            )

        version = data.get("version")
        if version != _SUPPORTED_VERSION:
            log.warning(
                "tfstate_unsupported_version",
                file_path=file_path,
                version=version,
                supported=_SUPPORTED_VERSION,
            )
            return ParsedDocument(
                sections=[],
                content_type=_TFSTATE_CONTENT_TYPE,
                language="json",
                metadata={
                    "parse_error": (
                        f"Unsupported state format version: {version!r} "
                        f"(only version {_SUPPORTED_VERSION} is supported)"
                    )
                },
            )

        try:
            sections = _parse_v4(data, file_path)
        except Exception as exc:
            log.error("tfstate_parse_failed", file_path=file_path, error=str(exc))
            return ParsedDocument(
                sections=[],
                content_type=_TFSTATE_CONTENT_TYPE,
                language="json",
                metadata={"parse_error": str(exc)},
            )

        doc_meta: dict[str, Any] = {
            "terraform_version": data.get("terraform_version", ""),
            "serial": data.get("serial"),
            "lineage": data.get("lineage", ""),
        }

        return ParsedDocument(
            sections=sections,
            content_type=_TFSTATE_CONTENT_TYPE,
            language="json",
            metadata=doc_meta,
        )


__all__ = ["TfStateParser"]
