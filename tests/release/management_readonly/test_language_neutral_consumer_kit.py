"""AC-SP86-6: the released consumer contract and severance kit remain
implementation-language neutral.

Shells out to the real Go toolchain (``go vet``/``go build``/``go test``)
against ``contracts/releases/management-readonly-v1/consumer-kit-go/`` -- a
clean Go consumer must validate schemas, fixtures, auth, freshness, and
severance decisions without executing Node.js or depending on a
TypeScript-only generated binding. An absent Go toolchain is an explicit
environment RED (``pytest.skip`` with a named reason), never a silently
green "not applicable".
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
KIT_DIR = ROOT / "contracts" / "releases" / "management-readonly-v1" / "consumer-kit-go"

_GO = shutil.which("go")


def _run_go(*args: str) -> subprocess.CompletedProcess[str]:
    assert _GO is not None
    return subprocess.run(
        [_GO, *args],
        cwd=KIT_DIR,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.skipif(_GO is None, reason="go toolchain not available on PATH (environment gate)")
def test_go_vet_is_clean() -> None:
    result = _run_go("vet", "./...")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(_GO is None, reason="go toolchain not available on PATH (environment gate)")
def test_go_build_is_clean(tmp_path: Path) -> None:
    result = _run_go("build", "-o", str(tmp_path / "consumer-kit"), "./...")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(_GO is None, reason="go toolchain not available on PATH (environment gate)")
def test_go_test_verifies_schemas_fixtures_auth_freshness_severance() -> None:
    """This IS the AC-SP86-6 probe: a clean Go consumer validating the
    release without Node.js or a TypeScript-only binding."""
    result = _run_go("test", "./...", "-v")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "TestConsumerKitVerifiesCleanRelease" in result.stdout


@pytest.mark.skipif(_GO is None, reason="go toolchain not available on PATH (environment gate)")
def test_go_verifies_exact_python_release_bindings(tmp_path: Path) -> None:
    producer = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "qualify_management_readonly_release.py")],
        cwd=ROOT,
        capture_output=True,
        check=False,
        timeout=120,
    )
    assert producer.returncode in (0, 1), producer.stderr
    lock_path = tmp_path / "release-lock.json"
    receipt_path = tmp_path / "receipt.json"
    lock_path.write_bytes(producer.stdout)
    pin = "sha256:" + hashlib.sha256(producer.stdout).hexdigest()
    now = datetime.now(UTC)
    receipt = {
        "schema": "management-readonly-v1-consumer-receipt-v1",
        "release_lock_digest": pin,
        "auth": {
            "token_profile": "omniscience-mcp-read-v1",
            "scopes": ["search"],
            "token_issued_at": (now - timedelta(minutes=2)).isoformat(),
            "token_expires_at": (now + timedelta(hours=1)).isoformat(),
        },
        "observed_at": (now - timedelta(minutes=1)).isoformat(),
        "expires_at": (now + timedelta(minutes=59)).isoformat(),
        "freshness_max_age_seconds": 3600,
    }
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    args = (
        "run",
        ".",
        "--release-lock",
        str(lock_path),
        "--release-lock-digest",
        pin,
        "--receipt",
        str(receipt_path),
    )
    result = _run_go(*args)
    assert result.returncode != 0
    expected = (
        "PENDING producer release qualification"
        if producer.returncode == 0
        else "release lock does not record clean GREEN producer checks"
    )
    assert expected in result.stderr, result.stdout + result.stderr
    assert "does not match local release files" not in result.stderr

    # Byte drift must be detected even when it preserves all fixture decisions.
    contracts = tmp_path / "contracts"
    shutil.copytree(ROOT / "contracts", contracts)
    copied_release = contracts / "releases" / "management-readonly-v1"
    fixture = copied_release / "severance-fixtures.json"
    fixture_bytes = fixture.read_bytes()
    fixture.write_bytes(fixture_bytes + b"\n")
    copied_args = (*args, "--release-root", str(copied_release))
    result = _run_go(*copied_args)
    assert result.returncode != 0
    assert "severance_fixture_digest does not match local release files" in result.stderr
    fixture.write_bytes(fixture_bytes)

    # A matching edit to a vendored pin cannot rewrite the release lock.
    mcp = contracts / "mcp"
    schema = mcp / "schemas" / "v1" / "contract-info.input.schema.json"
    schema_bytes = schema.read_bytes() + b"\n"
    schema.write_bytes(schema_bytes)
    pin_path = mcp / "pin.json"
    pin_data = json.loads(pin_path.read_bytes())
    for entry in pin_data["vendoredEntries"]:
        if entry["path"] == schema.relative_to(mcp).as_posix():
            entry["sha256"] = hashlib.sha256(schema_bytes).hexdigest()
            break
    else:
        pytest.fail("contract-info input schema is missing from the vendored pin")
    pin_path.write_text(json.dumps(pin_data), encoding="utf-8")
    result = _run_go(*copied_args)
    assert result.returncode != 0
    assert "schema_set_digest does not match local release files" in result.stderr

    lock_path.write_bytes(producer.stdout + b"\n")
    result = _run_go(*args)
    assert result.returncode != 0
    assert "release lock digest does not match the independently pinned digest" in result.stderr


@pytest.mark.skipif(_GO is None, reason="go toolchain not available on PATH (environment gate)")
def test_go_module_declares_zero_third_party_dependencies() -> None:
    """ADR-0022 OMR-9: no Node.js/TypeScript-only requirement -- and, for the
    Go side specifically, no third-party module dependency that could itself
    hide a supply-chain gate."""
    go_mod = (KIT_DIR / "go.mod").read_text(encoding="utf-8")
    assert "require" not in go_mod
    assert not (KIT_DIR / "go.sum").exists()


def test_kit_never_shells_out_to_node_or_npm() -> None:
    """Static proof independent of the Go toolchain's availability: no source
    file in the kit invokes node/npm/npx/tsc."""
    forbidden = ('"node"', '"npm"', '"npx"', '"tsc"', "node_modules")
    for path in sorted(KIT_DIR.rglob("*.go")):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{path}: forbidden token {token!r} found"
