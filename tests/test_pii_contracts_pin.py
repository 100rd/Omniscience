"""Contract-binding integrity: the vendored SP-60 schemas must match ``pin.json``.

Fails RED the moment any vendored schema under ``contracts/pii/schemas/v1`` drifts
from the digest this repository claims to bind to -- see ``contracts/pii/README.md``.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_ROOT = REPO_ROOT / "contracts" / "pii"


def _load_verify_pin():
    spec = importlib.util.spec_from_file_location(
        "contracts_pii_verify_pin", CONTRACT_ROOT / "tooling" / "verify_pin.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_vendored_schema_matches_its_pinned_digest() -> None:
    verify_pin = _load_verify_pin()
    errors = verify_pin.verify_pin(contract_root=CONTRACT_ROOT)
    assert errors == []


def test_pin_references_the_sp60_ground_truth_manifest_digest() -> None:
    pin = json.loads((CONTRACT_ROOT / "pin.json").read_text(encoding="utf-8"))
    assert pin["groundTruth"]["workPackage"] == "SP-60"
    assert pin["groundTruth"]["canonicalDigest"].startswith("sha256:")
    assert len(pin["vendoredEntries"]) > 0
