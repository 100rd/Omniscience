"""Tests for ADR, capability SPEC, and ready task governance validation."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def _validator() -> ModuleType:
    path = ROOT / "scripts" / "validate_governance.py"
    spec = importlib.util.spec_from_file_location("validate_governance", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_repository_governance_contracts_pass() -> None:
    assert _validator().validate_all() == []


def test_duplicate_adr_and_header_mismatch_fail(tmp_path: Path) -> None:
    (tmp_path / "0001-first.md").write_text("# ADR-0001: First\n- **Status**: Accepted\n")
    (tmp_path / "0001-second.md").write_text("# ADR-0002: Second\n- **Status**: Accepted\n")

    errors = _validator().validate_adrs(tmp_path)

    assert any("duplicate ADR-0001" in error for error in errors)
    assert any("H1 must declare ADR-0001" in error for error in errors)


def test_ready_task_rejects_tbd_and_string_criteria(tmp_path: Path) -> None:
    (tmp_path / "bad.md").write_text(
        """---
id: bad
title: Bad ready task
status: ready
source: {kind: github, ref: '1'}
governingAdrs: [ADR-0001]
capabilitySpecs: [SPEC-IN]
sddMode: standard
repo: /tmp/local/repo
scope: TBD
acceptanceCriteria: [it works]
rollback: {kind: revert-pr}
---
"""
    )

    errors = _validator().validate_tasks(tmp_path)

    assert any("contains TBD" in error for error in errors)
    assert any("absolute workstation path" in error for error in errors)
    assert any("criterion 1 must be structured" in error for error in errors)
    assert any("rollback requires kind and probe" in error for error in errors)
    assert any("readiness requires approvedBy and approvedAt" in error for error in errors)


def test_capability_requires_fallback_and_matching_probe(tmp_path: Path) -> None:
    (tmp_path / "SPEC-XX-example.md").write_text(
        """# SPEC-XX: Example
Status: draft
[REQ-XX-1] A requirement without its safety contract.
"""
    )

    errors = _validator().validate_capabilities(tmp_path)

    assert any("REQ-XX-1 has no Fallback" in error for error in errors)
    assert any("REQ-XX-1 has no matching probe" in error for error in errors)


def test_ready_capability_requires_accepted_adr_provenance_and_codeowner(
    tmp_path: Path,
) -> None:
    specs = tmp_path / "specs"
    adrs = tmp_path / "decisions"
    specs.mkdir()
    adrs.mkdir()
    (specs / "SPEC-XX-example.md").write_text(
        """# SPEC-XX: Example
Status: ready
[REQ-XX-1] A requirement. **Fallback:** stop.
## Probes
- P-XX-1: passes
"""
    )
    (adrs / "0019-example.md").write_text("# ADR-0019: Example\n- **Status**: Proposed\n")
    codeowners = tmp_path / "CODEOWNERS"
    codeowners.write_text("/specs/ @someone-else\n")

    errors = _validator().validate_capabilities(
        specs,
        adr_directory=adrs,
        codeowners_path=codeowners,
    )

    assert any("no human readiness provenance" in error for error in errors)
    assert any("requires accepted ADR-0019" in error for error in errors)

    (specs / "SPEC-XX-example.md").write_text(
        """# SPEC-XX: Example
Status: ready
Readiness: human-approved by @owner on 2026-07-11 under accepted ADR-0019
[REQ-XX-1] A requirement. **Fallback:** stop.
## Probes
- P-XX-1: passes
"""
    )
    (adrs / "0019-example.md").write_text("# ADR-0019: Example\n- **Status**: Accepted\n")

    errors = _validator().validate_capabilities(
        specs,
        adr_directory=adrs,
        codeowners_path=codeowners,
    )

    assert any("readiness owner @owner does not own /specs/" in error for error in errors)
