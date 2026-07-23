"""Minimal, dependency-free JSON Schema subset validator (task-sp-81).

Rationale: this repository declares no third-party Python dependency for schema
validation (no ``jsonschema`` in ``pyproject.toml``). This module implements the same
small subset of JSON Schema (2020-12 vocabulary) as ``contracts/pii/validator/``:
``type``, ``required``, ``properties``, ``additionalProperties``, ``enum``, ``const``,
``pattern``, ``minLength``, ``minItems``, ``items``, and a same-directory ``$ref``.

It is intentionally not a general-purpose JSON Schema implementation. Unlike
``contracts/pii/validator/``, this copy is originally authored for SPEC-MCTX -- there
is no external SP-81 ground-truth manifest to bind to (ADR-0021: Omniscience owns the
``ManagementContextBundle``/``KnowledgeQualitySnapshot`` shape itself).
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
    """Validate ``instance`` against ``schema``. Returns a list of error strings (empty = valid)."""
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
            errors.append(f"{path}: expected type '{schema_type}', got {type(instance).__name__}")
            return errors

    if schema_type == "string" and isinstance(instance, str):
        if "pattern" in schema and re.fullmatch(schema["pattern"], instance) is None:
            errors.append(f"{path}: value {instance!r} does not match pattern {schema['pattern']!r}")
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: value {instance!r} shorter than minLength {schema['minLength']}")

    if schema_type == "array" and isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: array has {len(instance)} items, fewer than minItems {schema['minItems']}")
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
