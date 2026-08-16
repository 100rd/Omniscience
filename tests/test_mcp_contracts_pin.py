"""Contract-binding integrity: the vendored SP-10 MCP artifacts must match ``pin.json``.

Fails RED the moment any vendored artifact under ``contracts/mcp`` drifts from the
digest this repository claims to bind to, or the moment the closure goes missing --
see ``contracts/mcp/README.md``. This closure is one of the three release-contract
pins ``/ready`` gates on (``apps/server/src/omniscience_server/routes/health.py``
``_check_contract_pin("mcp", "vendoredEntries")``); it shipped to ``main`` in the
SP-95 health check but the SP-86 closure it consumes was orphaned, leaving
``/ready`` permanently ``mcp_contract_unavailable``. This test guards against that
regression recurring.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_ROOT = REPO_ROOT / "contracts" / "mcp"


def _load_verify_pin():
    spec = importlib.util.spec_from_file_location(
        "contracts_mcp_verify_pin", CONTRACT_ROOT / "tooling" / "verify_pin.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_vendored_artifact_matches_its_pinned_digest_and_source() -> None:
    verify_pin = _load_verify_pin()
    errors = verify_pin.verify_pin(contract_root=CONTRACT_ROOT)
    assert errors == []


def test_pin_binds_the_sp10_mcp_ground_truth() -> None:
    pin = json.loads((CONTRACT_ROOT / "pin.json").read_text(encoding="utf-8"))
    assert pin["boundPackage"] == "task-sp-86-management-readonly-release"
    ground_truth = pin["groundTruth"]
    assert ground_truth["workPackage"] == "SP-10"
    schema_set = ground_truth["schemaSetSha256"]
    assert isinstance(schema_set, str) and len(schema_set) == 64
    assert len(pin["vendoredEntries"]) > 0


def test_readiness_probe_key_is_present_and_truthy() -> None:
    # /ready calls _check_contract_pin("mcp", "vendoredEntries"); this asserts the
    # exact key that probe requires is present and non-empty in the shipped pin.
    pin = json.loads((CONTRACT_ROOT / "pin.json").read_text(encoding="utf-8"))
    assert pin.get("vendoredEntries")
