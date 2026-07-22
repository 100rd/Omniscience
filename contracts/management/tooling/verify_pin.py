"""Recompute sha256 of every SP-81 management-context schema and compare to pin.json.

Pure, offline, read-only: reads files under contracts/management/ and reports the
result. Unlike ``contracts/pii/tooling/verify_pin.py`` this binds to no external
ground-truth manifest -- ADR-0021 makes Omniscience the schema owner -- so it only
proves the schemas actually committed here have not silently drifted from what
``pin.json`` (and every fixture digest derived from it) claims. Run this after
editing any schema under ``schemas/v1``.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

CONTRACT_ROOT = Path(__file__).resolve().parents[1]


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_pin(*, contract_root: Path = CONTRACT_ROOT) -> list[str]:
    """Return a list of mismatch/missing-file errors (empty means clean)."""
    pin = json.loads((contract_root / "pin.json").read_text(encoding="utf-8"))
    errors: list[str] = []
    for entry in pin["ownedEntries"]:
        path = contract_root / entry["path"]
        if not path.exists():
            errors.append(f"missing schema file: {entry['path']}")
            continue
        actual = _sha256_of(path)
        if actual != entry["sha256"]:
            errors.append(
                f"digest drift: {entry['path']} sha256={actual} != pinned {entry['sha256']}"
            )
    return errors


def main() -> int:
    errors = verify_pin()
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)  # noqa: T201 -- one-shot CLI report
        return 1
    print("OK: every SP-81 management-context schema matches its pinned digest")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
