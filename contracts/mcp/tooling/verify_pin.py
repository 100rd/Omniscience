"""Recompute sha256 of every vendored SP-10 MCP artifact and compare to pin.json.

Pure, offline, read-only: reads files under ``contracts/mcp/`` plus the live
ground-truth SP-10 contract at ``apps/server/src/omniscience_server/mcp/contracts/v1/``
(see ``../pin.json`` and ``../README.md``). Two independent checks:

1. every vendored copy under ``contracts/mcp/`` matches its pinned digest
   (catches accidental local edits to the copy itself);
2. every vendored copy is byte-identical to the live ground-truth file at the
   same relative path (catches SP-10 drifting without this binding being
   re-vendored -- task-sp-86-management-readonly-release cannot edit the SP-10
   implementation, so drift here must fail RED, never be silently accepted).
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

CONTRACT_ROOT = Path(__file__).resolve().parents[1]
GROUND_TRUTH_ROOT = (
    CONTRACT_ROOT.parents[1]
    / "apps"
    / "server"
    / "src"
    / "omniscience_server"
    / "mcp"
    / "contracts"
    / "v1"
)


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ground_truth_relative_path(vendored_relative_path: str) -> str:
    """Map a vendored ``contracts/mcp/`` path to its SP-10 ground-truth path.

    The vendored copy nests schemas one level deeper (``schemas/v1/<name>``, to
    match ``contracts/management`` and ``contracts/pii``'s own layout) than the
    SP-10 source (flat ``schemas/<name>``, see
    ``apps/server/src/omniscience_server/mcp/materialize.py::_schema_path``).
    ``manifest.json``/``tool-registry.json`` map 1:1 at the contract root.
    """
    if vendored_relative_path.startswith("schemas/v1/"):
        return "schemas/" + vendored_relative_path.removeprefix("schemas/v1/")
    return vendored_relative_path


def verify_pin(
    *, contract_root: Path = CONTRACT_ROOT, ground_truth_root: Path = GROUND_TRUTH_ROOT
) -> list[str]:
    """Return a list of mismatch/missing-file errors (empty means clean)."""
    pin = json.loads((contract_root / "pin.json").read_text(encoding="utf-8"))
    errors: list[str] = []
    for entry in pin["vendoredEntries"]:
        vendored_path = contract_root / entry["path"]
        if not vendored_path.is_file():
            errors.append(f"missing vendored file: {entry['path']}")
            continue
        actual = _sha256_of(vendored_path)
        if actual != entry["sha256"]:
            errors.append(
                f"pin digest drift: {entry['path']} sha256={actual} != pinned {entry['sha256']}"
            )

        ground_truth_relative = _ground_truth_relative_path(entry["path"])
        ground_truth_path = ground_truth_root / ground_truth_relative
        if not ground_truth_path.is_file():
            errors.append(f"ground-truth file absent: {ground_truth_relative}")
            continue
        if _sha256_of(ground_truth_path) != actual:
            errors.append(f"ground-truth drift: {entry['path']} no longer matches SP-10 source")

    return errors


def main() -> int:
    errors = verify_pin()
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)  # noqa: T201 -- one-shot CLI report
        return 1
    print("OK: every vendored SP-10 MCP artifact matches pin + source")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
