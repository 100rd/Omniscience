"""Loader for the vendored SP-60 schema binding under ``contracts/pii/`` (task-sp-61).

Delegates structural validation to the vendored, dependency-free
``contracts/pii/validator/schema_check.py`` -- this module never re-implements JSON
Schema semantics, it only locates and loads that file. See ``contracts/pii/README.md``.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType
from typing import Any


def discover_contract_root() -> Path:
    """Walk upward from this file to find the repository's ``contracts/pii`` directory.

    ``OMNISCIENCE_PII_CONTRACT_ROOT`` overrides discovery for out-of-tree test layouts.
    """
    override = os.environ.get("OMNISCIENCE_PII_CONTRACT_ROOT")
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "contracts" / "pii"
        if candidate.is_dir():
            return candidate
    raise RuntimeError(
        "contracts/pii not found above omniscience_core.privacy; "
        "set OMNISCIENCE_PII_CONTRACT_ROOT to override"
    )


def _load_vendored_schema_check(contract_root: Path) -> ModuleType:
    module_path = contract_root / "validator" / "schema_check.py"
    spec = importlib.util.spec_from_file_location(
        "omniscience_core.privacy._vendored_schema_check", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load vendored schema_check.py from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_CONTRACT_ROOT = discover_contract_root()
_SCHEMA_CHECK = _load_vendored_schema_check(_CONTRACT_ROOT)


def validate_against_schema(instance: Any, schema_name: str) -> list[str]:
    """Validate ``instance`` against a vendored SP-60 schema by filename."""
    schema = _SCHEMA_CHECK.load_schema(schema_name)
    errors: list[str] = _SCHEMA_CHECK.validate(instance, schema)
    return errors
