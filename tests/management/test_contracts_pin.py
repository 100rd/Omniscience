"""Contract integrity: every SP-81 management-context schema must match ``pin.json``.

Fails RED the moment any schema under ``contracts/management/schemas/v1`` drifts
from the digest this repository freezes -- see ``contracts/management/README.md``.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = REPO_ROOT / "contracts" / "management"


def _load_verify_pin():
    spec = importlib.util.spec_from_file_location(
        "contracts_management_verify_pin", CONTRACT_ROOT / "tooling" / "verify_pin.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_schema_matches_its_pinned_digest() -> None:
    verify_pin = _load_verify_pin()
    errors = verify_pin.verify_pin(contract_root=CONTRACT_ROOT)
    assert errors == []


def test_pin_declares_the_sp81_governing_adrs() -> None:
    pin = json.loads((CONTRACT_ROOT / "pin.json").read_text(encoding="utf-8"))
    assert pin["boundPackage"] == "task-sp-81-management-context-v1"
    assert "Omniscience/ADR-0021" in pin["governingAdrs"]
    assert len(pin["ownedEntries"]) > 0
