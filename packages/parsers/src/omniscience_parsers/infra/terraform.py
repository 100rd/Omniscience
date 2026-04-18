"""Terraform parser — extracts resources, data sources, modules, and variables from .tf files.

HCL is parsed with a lightweight regex-based approach that handles the common structural
patterns used in real Terraform codebases without requiring a full HCL grammar.

Each top-level block becomes a :class:`~omniscience_parsers.base.Section` with:
- ``symbol``:  ``"resource.aws_s3_bucket.my_bucket"`` (block_type.type.name)
- ``metadata``: ``{"block_type": ..., "resource_type": ..., "depends_on": [...], "refs": [...]}``

Dependency edges are captured in metadata so the graph extractor can consume them.
"""

from __future__ import annotations

import json
import re
from typing import Any

import structlog

from omniscience_parsers.base import ParsedDocument, Section

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Supported file extensions and content types
# ---------------------------------------------------------------------------

_TF_EXTENSIONS = frozenset({".tf", ".tf.json"})
_TF_CONTENT_TYPES = frozenset(
    {
        "application/x-terraform",
        "text/x-terraform",
        "application/hcl",
    }
)

# ---------------------------------------------------------------------------
# Regex patterns for HCL block parsing
# ---------------------------------------------------------------------------

# Matches: resource "aws_s3_bucket" "my_bucket" {
#          data    "aws_iam_policy" "current" {
#          module  "vpc" {
#          variable "instance_type" {
#          output  "bucket_arn" {
#          locals  {
_BLOCK_HEADER = re.compile(
    r'^(?P<block_type>resource|data|module|variable|output|locals|terraform|provider)'
    r'(?:\s+"(?P<label1>[^"]+)")?'
    r'(?:\s+"(?P<label2>[^"]+)")?'
    r'\s*\{',
    re.MULTILINE,
)

# Reference to another resource/data in an attribute value:
# aws_s3_bucket.my_bucket.arn  or  data.aws_iam_policy.current.id
_RESOURCE_REF = re.compile(
    r'\b(?P<ref_type>resource|data)?\.?'
    r'(?P<rtype>[a-z][a-z0-9_]+(?:\.[a-z][a-z0-9_]+)+)'
)

# Explicit interpolation reference: ${aws_s3_bucket.my_bucket.arn}
_INTERPOLATION_REF = re.compile(
    r'\$\{(?P<expr>[^}]+)\}'
)

# depends_on = [resource.name, ...]
_DEPENDS_ON = re.compile(
    r'depends_on\s*=\s*\[(?P<items>[^\]]*)\]',
    re.DOTALL,
)

# Terraform reference expression like: aws_s3_bucket.example  or  module.vpc
_PLAIN_REF = re.compile(
    r'\b(?P<kind>(?:resource|data|module)\.)?'
    r'(?P<rtype>[a-z][a-z0-9_]+)\.'
    r'(?P<rname>[a-z][a-z0-9_-]+)'
    r'(?:\.[a-z][a-z0-9_]+)*\b'
)


def _extract_block_content(src: str, start_brace: int) -> tuple[str, int]:
    """Return the content inside balanced braces starting at *start_brace* and the end index."""
    depth = 0
    i = start_brace
    in_string = False
    escape = False
    while i < len(src):
        ch = src[i]
        if escape:
            escape = False
        elif ch == "\\" and in_string:
            escape = True
        elif ch == '"' and not in_string:
            in_string = True
        elif ch == '"' and in_string:
            in_string = False
        elif not in_string:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return src[start_brace + 1 : i], i
        i += 1
    # Unclosed block — return everything remaining
    return src[start_brace + 1 :], len(src) - 1


def _extract_depends_on(block_body: str) -> list[str]:
    """Return explicit depends_on references from a block body."""
    deps: list[str] = []
    m = _DEPENDS_ON.search(block_body)
    if not m:
        return deps
    items_str = m.group("items")
    for item in re.split(r"[,\s]+", items_str):
        item = item.strip().strip('"')
        if item:
            deps.append(item)
    return deps


def _extract_implicit_refs(block_body: str) -> list[str]:
    """Return implicit resource references found in interpolations and attribute values."""
    refs: set[str] = set()

    # Scan interpolation expressions: ${...}
    for interp_m in _INTERPOLATION_REF.finditer(block_body):
        expr = interp_m.group("expr")
        for ref_m in _PLAIN_REF.finditer(expr):
            candidate = f"{ref_m.group('rtype')}.{ref_m.group('rname')}"
            refs.add(candidate)

    # Also scan plain attribute values (outside interpolation) for foo.bar.attr patterns
    # Remove string delimiters first to avoid false positives from comments
    stripped = re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"', " STRING ", block_body)
    for ref_m in _PLAIN_REF.finditer(stripped):
        rtype = ref_m.group("rtype")
        rname = ref_m.group("rname")
        # Exclude common false positives like "var.xxx", "local.xxx", "path.xxx"
        if rtype in ("var", "local", "locals", "path", "self", "each", "count", "terraform"):
            continue
        candidate = f"{rtype}.{rname}"
        refs.add(candidate)

    return sorted(refs)


def _symbol_for_block(block_type: str, label1: str | None, label2: str | None) -> str:
    """Build a canonical symbol string for a Terraform block."""
    if block_type == "resource" and label1 and label2:
        return f"resource.{label1}.{label2}"
    if block_type == "data" and label1 and label2:
        return f"data.{label1}.{label2}"
    if block_type == "module" and label1:
        return f"module.{label1}"
    if block_type == "variable" and label1:
        return f"variable.{label1}"
    if block_type == "output" and label1:
        return f"output.{label1}"
    if block_type == "provider" and label1:
        return f"provider.{label1}"
    return block_type


def _heading_path_for_block(
    block_type: str, label1: str | None, label2: str | None
) -> list[str]:
    """Build a human-readable heading path for a block."""
    parts: list[str] = [block_type]
    if label1:
        parts.append(label1)
    if label2:
        parts.append(label2)
    return parts


# ---------------------------------------------------------------------------
# JSON (.tf.json) support
# ---------------------------------------------------------------------------


def _parse_tf_json(content: bytes, file_path: str) -> ParsedDocument:
    """Parse a .tf.json file into sections."""
    sections: list[Section] = []
    try:
        data: dict[str, Any] = json.loads(content.decode(errors="replace"))
    except json.JSONDecodeError as exc:
        log.warning("terraform_json_parse_error", file_path=file_path, error=str(exc))
        return ParsedDocument(
            sections=[],
            content_type="application/x-terraform",
            language="terraform",
            metadata={"parse_error": str(exc)},
        )

    line_cursor = 1  # approximate

    for block_type, block_val in data.items():
        if not isinstance(block_val, dict):
            continue
        if block_type in ("resource", "data"):
            for rtype, instances in block_val.items():
                if not isinstance(instances, dict):
                    continue
                for rname, body in instances.items():
                    symbol = f"{block_type}.{rtype}.{rname}"
                    body_text = json.dumps({rname: body}, indent=2)
                    line_count = body_text.count("\n") + 1
                    depends_on = body.get("depends_on", []) if isinstance(body, dict) else []
                    sections.append(
                        Section(
                            heading_path=[block_type, rtype, rname],
                            text=body_text,
                            line_start=line_cursor,
                            line_end=line_cursor + line_count - 1,
                            symbol=symbol,
                            metadata={
                                "block_type": block_type,
                                "resource_type": rtype,
                                "resource_name": rname,
                                "depends_on": depends_on,
                                "refs": [],
                            },
                        )
                    )
                    line_cursor += line_count
        elif block_type == "module":
            for mname, body in block_val.items():
                symbol = f"module.{mname}"
                body_text = json.dumps({mname: body}, indent=2)
                line_count = body_text.count("\n") + 1
                sections.append(
                    Section(
                        heading_path=["module", mname],
                        text=body_text,
                        line_start=line_cursor,
                        line_end=line_cursor + line_count - 1,
                        symbol=symbol,
                        metadata={
                            "block_type": "module",
                            "depends_on": [],
                            "refs": [],
                        },
                    )
                )
                line_cursor += line_count
        elif block_type == "variable":
            for vname, body in block_val.items():
                symbol = f"variable.{vname}"
                body_text = json.dumps({vname: body}, indent=2)
                line_count = body_text.count("\n") + 1
                sections.append(
                    Section(
                        heading_path=["variable", vname],
                        text=body_text,
                        line_start=line_cursor,
                        line_end=line_cursor + line_count - 1,
                        symbol=symbol,
                        metadata={
                            "block_type": "variable",
                            "depends_on": [],
                            "refs": [],
                        },
                    )
                )
                line_cursor += line_count

    return ParsedDocument(
        sections=sections,
        content_type="application/x-terraform",
        language="terraform",
    )


# ---------------------------------------------------------------------------
# HCL (.tf) parser
# ---------------------------------------------------------------------------


def _parse_tf_hcl(content: bytes, file_path: str) -> ParsedDocument:
    """Parse an HCL .tf file into sections using regex block detection."""
    src = content.decode(errors="replace")
    lines = src.splitlines()
    sections: list[Section] = []

    for match in _BLOCK_HEADER.finditer(src):
        block_type = match.group("block_type")
        label1 = match.group("label1")
        label2 = match.group("label2")

        # Find the opening brace position
        brace_pos = src.index("{", match.start())
        block_body, block_end = _extract_block_content(src, brace_pos)

        # Compute line numbers
        line_start = src[: match.start()].count("\n") + 1
        line_end = src[:block_end].count("\n") + 1

        block_text = src[match.start() : block_end + 1]

        # Build symbol and heading path
        symbol = _symbol_for_block(block_type, label1, label2)
        heading_path = _heading_path_for_block(block_type, label1, label2)

        # Extract dependencies
        depends_on = _extract_depends_on(block_body)
        refs = _extract_implicit_refs(block_body)

        meta: dict[str, Any] = {
            "block_type": block_type,
            "depends_on": depends_on,
            "refs": refs,
        }
        if label1:
            if block_type in ("resource", "data"):
                meta["resource_type"] = label1
                if label2:
                    meta["resource_name"] = label2
            elif block_type == "module":
                meta["module_name"] = label1
            elif block_type == "variable":
                meta["variable_name"] = label1

        sections.append(
            Section(
                heading_path=heading_path,
                text=block_text,
                line_start=line_start,
                line_end=line_end,
                symbol=symbol,
                metadata=meta,
            )
        )

    return ParsedDocument(
        sections=sections,
        content_type="application/x-terraform",
        language="terraform",
        metadata={"source_lines": len(lines)},
    )


# ---------------------------------------------------------------------------
# TerraformParser
# ---------------------------------------------------------------------------


class TerraformParser:
    """Parse Terraform configuration files (.tf, .tf.json) into structured sections.

    Each top-level block (resource, data, module, variable, output) becomes
    a :class:`~omniscience_parsers.base.Section` with a canonical ``symbol``
    and dependency metadata that the graph extractor can consume.
    """

    def can_handle(self, content_type: str, file_extension: str) -> bool:
        ext = file_extension.lower()
        return content_type in _TF_CONTENT_TYPES or ext in _TF_EXTENSIONS or ext == ".tf"

    def parse(self, content: bytes, file_path: str = "") -> ParsedDocument:
        """Parse *content* as a Terraform file.

        The parser auto-detects whether the content is JSON (.tf.json) or HCL (.tf)
        based on the file extension.  JSON files are parsed with ``json.loads``;
        HCL files use a regex-based block extractor.
        """
        is_json = file_path.endswith(".tf.json") or (
            not file_path.endswith(".tf") and content.strip().startswith(b"{")
        )

        try:
            if is_json:
                return _parse_tf_json(content, file_path)
            return _parse_tf_hcl(content, file_path)
        except Exception as exc:
            log.error(
                "terraform_parse_failed",
                file_path=file_path,
                error=str(exc),
            )
            return ParsedDocument(
                sections=[],
                content_type="application/x-terraform",
                language="terraform",
                metadata={"parse_error": str(exc)},
            )


__all__ = ["TerraformParser"]
