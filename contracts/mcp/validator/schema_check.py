"""Minimal, dependency-free JSON Schema subset validator (task-sp-86).

Rationale: this repository declares no third-party Python dependency for schema
validation (no ``jsonschema`` in ``pyproject.toml``). This module implements the same
small subset of JSON Schema (2020-12 vocabulary) as ``contracts/pii/validator/`` and
``contracts/management/validator/``: ``type``, ``required``, ``properties``,
``additionalProperties``, ``enum``, ``const``, ``pattern``, ``minLength``,
``minItems``, ``items``, and a same-directory ``$ref``.

It is intentionally not a general-purpose JSON Schema implementation. This copy
validates the vendored SP-10 MCP schemas under ``../schemas/v1`` -- neither has a
signed external ground truth other than the already-merged, out-of-scope
apps/server MCP implementation (see ``../pin.json``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas" / "v1"

_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}

_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}


def load_schema(name: str) -> dict[str, Any]:
    """Load and cache a schema document by filename from ``schemas/v1``."""
    if name not in _SCHEMA_CACHE:
        path = SCHEMA_DIR / name
        with path.open("r", encoding="utf-8") as handle:
            _SCHEMA_CACHE[name] = json.load(handle)
    return _SCHEMA_CACHE[name]


def validate(instance: Any, schema: dict[str, Any], *, path: str = "$") -> list[str]:
    """Validate ``instance`` against ``schema``.

    Returns a list of error strings (empty means valid).
    """
    errors: list[str] = []

    if "$ref" in schema:
        try:
            referenced = load_schema(schema["$ref"])
        except (OSError, json.JSONDecodeError) as exc:
            return [f"{path}: unresolved $ref '{schema['$ref']}': {exc}"]
        return validate(instance, referenced, path=path)

    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}, got {instance!r}")

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: value {instance!r} is not one of {schema['enum']!r}")

    schema_type = schema.get("type")
    if schema_type is not None:
        allowed = _TYPE_MAP.get(schema_type)
        if allowed is None:
            errors.append(f"{path}: unknown schema type '{schema_type}'")
        elif (
            not isinstance(instance, allowed)
            or (schema_type == "integer" and isinstance(instance, bool))
            or (schema_type == "number" and isinstance(instance, bool))
        ):
            got = type(instance).__name__
            errors.append(f"{path}: expected type '{schema_type}', got {got}")
            return errors

    if schema_type == "string" and isinstance(instance, str):
        pattern = schema.get("pattern")
        if pattern is not None and re.fullmatch(pattern, instance) is None:
            errors.append(f"{path}: value {instance!r} does not match pattern {pattern!r}")
        min_length = schema.get("minLength")
        if min_length is not None and len(instance) < min_length:
            errors.append(f"{path}: value {instance!r} shorter than minLength {min_length}")

    if schema_type == "array" and isinstance(instance, list):
        min_items = schema.get("minItems")
        if min_items is not None and len(instance) < min_items:
            count = len(instance)
            errors.append(f"{path}: array has {count} items, fewer than minItems {min_items}")
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(instance):
                errors.extend(validate(item, item_schema, path=f"{path}[{index}]"))

    if schema_type == "object" and isinstance(instance, dict):
        for required_key in schema.get("required", []):
            if required_key not in instance:
                errors.append(f"{path}: missing required field '{required_key}'")
        properties = schema.get("properties", {})
        for key, value in instance.items():
            if key in properties:
                errors.extend(validate(value, properties[key], path=f"{path}.{key}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unknown field '{key}' is not permitted by this schema")

    return errors
