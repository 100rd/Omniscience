#!/usr/bin/env python3
"""Management-readonly-v1 release-lock qualification harness (AC-SP86-1).

Free-standing, framework-free harness that re-derives the
``OmniscienceManagementReadOnlyRelease`` digests from the current worktree and
Git history and returns a typed GREEN/RED decision -- never an uncaught
exception, never a fabricated GREEN. Mirrors the ``VerificationResult``
pattern already used by ``scripts/qualify_backup_restore.py`` and
``scripts/check_consumer_severance.py``.

This module NEVER builds or pushes a real container image, NEVER opens a
network connection, and NEVER deploys anything. AC-SP86-1 requires "Git image
chart MCP context schema PW0 policy dependency and rollback digests" to
"re-derive exactly" with "mutable or dirty inputs" RED -- the live OCI
registry push named in the task spec's ``evidenceDestination`` is an external
gate this script only records as ``pending`` (see ``pending.image_registry_digest``
in the built release lock and ``contracts/releases/management-readonly-v1/README.md``
"External / pending gates").

Public surface
--------------
VerificationResult        -- frozen GREEN/RED decision-return, never raised
build_release_lock        -- AC-SP86-1: build + self-validate the full release lock
verify_severance_receipt  -- AC-SP86-4: read-only consumer severance receipt verifier

What this module deliberately does NOT do
------------------------------------------
It does not exercise AC-SP86-2 (runtime conformance), AC-SP86-3 (PW0 seeded-leak),
AC-SP86-5 (ops/rollback drill), or AC-SP86-6 (Go consumer kit `go test`) itself --
those are exercised as pytest suites under ``tests/release/management_readonly/``
and ``contracts/releases/management-readonly-v1/consumer-kit-go/`` respectively
(see ``docs/runbooks/management-readonly-release.md``). This script computes and
self-validates the release-lock digests those suites' evidence feeds into.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

PROFILE_ID = "management-readonly-v1"
PROFILE_REVISION = "1.0.0"
AUTH_TOKEN_PROFILE = "omniscience-mcp-read-v1"  # noqa: S105 - public profile identifier

#: ADR-0022 / SP-60 pinned policy digest (see docs/decisions/0022-*.md and
#: contracts/pii/pin.json groundTruth.canonicalDigest, which this script
#: cross-checks against this exact literal on every run -- a change to either
#: side without updating the other is a RED, never a silent re-pin).
PINNED_SP60_POLICY_DIGEST = (
    "sha256:fa312387e5fa4b3980b085e20033fc8d947474c62c15dd9e3836372dea01f038"
)

#: SP-61's own fixture/consumer-pin convention (tests/privacy/fixtures.py
#: policy_bundle_fixture()["policy_revision"]). No live SP-60 policy bundle
#: exists in this repository (task boundary: "no active policy, signing key,
#: permit, or provider call", contracts/pii/README.md) -- this is the
#: documented non-live revision string every SP-61 fixture already pins to.
PII_POLICY_REVISION = "1.0.0"

#: The task spec's own writable-scope include list (docs/specs/task-sp-86-*.md
#: front matter `scope.include`), used to scope the Git-cleanliness check so
#: this harness never flags the ~420 pre-existing, unrelated dirty entries
#: (graphify-out/ cache and friends) that this task must preserve untouched.
SCOPED_PATHS: tuple[str, ...] = (
    "Dockerfile",
    ".dockerignore",
    "pyproject.toml",
    "uv.lock*",
    # Dockerfile copies these complete trees into the image. Scope cleanliness
    # must cover every source, contract, auth/config module, and package
    # manifest that can affect the built artifact.
    "apps",
    "packages",
    "contracts",
    "helm/omniscience",
    "scripts/qualify_management_readonly_release.py",
    "tests/release/management_readonly",
    "tests/test_management_readonly_release_lock.py",
    "docs/runbooks/management-readonly-release.md",
    ".github/workflows/management-readonly-release.yml",
)


@dataclass(frozen=True)
class VerificationResult:
    """A decision return -- never an exception, always GREEN or RED."""

    status: str  # "GREEN" | "RED"
    reasons: tuple[str, ...] = ()

    @property
    def is_green(self) -> bool:
        return self.status == "GREEN"


def _green() -> VerificationResult:
    return VerificationResult("GREEN")


def _red(*reasons: str) -> VerificationResult:
    return VerificationResult("RED", reasons)


# ---------------------------------------------------------------------------
# Pure digest helpers
# ---------------------------------------------------------------------------


def _sha256_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _sha256_tree(paths: Iterable[Path], *, root: Path) -> str:
    """Composite digest over a set of files: sha256 of sorted "path:sha256\\n" rows.

    Same construction as ``apps/server/.../mcp/materialize.py``'s
    ``schema_set_sha256`` -- content-addressed and independent of filesystem
    iteration order.
    """
    rows: list[str] = []
    for path in paths:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        rows.append(f"{relative}:{digest}\n")
    canonical = "".join(sorted(rows)).encode("utf-8")
    return _sha256_bytes(canonical)


def _composite_digest(entries: Iterable[tuple[str, str]]) -> str:
    """Composite digest over already-known (path, sha256-hex) pairs."""
    rows = sorted(f"{path}:{digest}\n" for path, digest in entries)
    return _sha256_bytes("".join(rows).encode("utf-8"))


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


class GitUnavailableError(RuntimeError):
    """Raised when ``git`` cannot be located or invoked -- callers treat this
    as a RED input, never a silent skip."""


def _git(repo: Path, *args: str) -> str:
    git = shutil.which("git")
    if git is None:
        raise GitUnavailableError("git executable not found on PATH")
    try:
        result = subprocess.run(  # noqa: S603 - fixed executable; args are internal/validated
            [git, *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise GitUnavailableError(str(exc)) from exc
    return result.stdout


def _current_commit(repo: Path) -> str | None:
    try:
        return _git(repo, "rev-parse", "HEAD").strip().lower()
    except GitUnavailableError:
        return None


def _scoped_worktree_is_clean(repo: Path, scoped_paths: Sequence[str]) -> bool | None:
    """Return True/False, or None when Git itself is unavailable (never
    silently treated as clean)."""
    try:
        status = _git(
            repo,
            "-c",
            "core.quotepath=false",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            *scoped_paths,
        )
    except GitUnavailableError:
        return None
    return status.strip() == ""


# ---------------------------------------------------------------------------
# Dynamic loaders for the sibling contracts/*/tooling/verify_pin.py modules
# ---------------------------------------------------------------------------


def _load_module(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _verify_contract_pin(repo: Path, contract_dir: str) -> list[str]:
    module_path = repo / "contracts" / contract_dir / "tooling" / "verify_pin.py"
    if not module_path.is_file():
        return [f"missing_verify_pin_tooling:{contract_dir}"]
    module = _load_module(module_path, f"_qualify_verify_pin_{contract_dir}")
    result: list[str] = module.verify_pin()
    return result


# ---------------------------------------------------------------------------
# AC-SP86-1: release lock
# ---------------------------------------------------------------------------


def _pin_entries(pin_path: Path, *, key: str) -> list[tuple[str, str]]:
    pin = json.loads(pin_path.read_text(encoding="utf-8"))
    return [(entry["path"], entry["sha256"]) for entry in pin[key]]


def _image_digest(repo: Path) -> str:
    """Legacy build-recipe digest over Dockerfile bytes.

    Not the live OCI registry digest -- that only exists after a real
    ``docker build`` + ``docker push`` runs in CI (external gate; see
    ``contracts/releases/management-readonly-v1/README.md``). Kept under its
    historical field name for compatibility; ``source_input_digest`` covers
    the actual source inputs copied into the build.
    """
    return _sha256_file(repo / "Dockerfile")


SOURCE_INPUT_PATHS: tuple[str, ...] = (
    "Dockerfile",
    ".dockerignore",
    "pyproject.toml",
    "uv.lock*",
    "apps",
    "packages",
    "contracts",
)


def _source_input_digest(repo: Path) -> str | None:
    """Hash tracked and non-ignored untracked files copied into the image.

    Paths are sorted and combined with their byte digests. Git's standard
    ignore rules keep build caches such as ``.venv`` and graph outputs out.
    """
    try:
        output = _git(
            repo,
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            *SOURCE_INPUT_PATHS,
        )
    except GitUnavailableError:
        return None
    entries: list[tuple[str, str]] = []
    for raw_path in output.split("\0"):
        if not raw_path:
            continue
        path = repo / raw_path
        if path.is_file():
            entries.append((raw_path, hashlib.sha256(path.read_bytes()).hexdigest()))
    if not entries:
        return None
    return _composite_digest(entries)


def _chart_digest(repo: Path) -> str:
    chart_root = repo / "helm" / "omniscience"
    files = sorted(p for p in chart_root.rglob("*") if p.is_file())
    return _sha256_tree(files, root=repo)


def _configuration_digest(repo: Path) -> str:
    surface = [
        repo / "apps" / "server" / "src" / "omniscience_server" / "app.py",
        repo / "apps" / "server" / "src" / "omniscience_server" / "routes" / "health.py",
        repo / "helm" / "omniscience" / "values.yaml",
    ]
    existing = [p for p in surface if p.is_file()]
    return _sha256_tree(existing, root=repo)


def _mcp_manifest_digest(repo: Path, reasons: list[str]) -> str:
    pin_errors = _verify_contract_pin(repo, "mcp")
    if pin_errors:
        reasons.extend(f"mcp_pin:{e}" for e in pin_errors)
    entries = _pin_entries(repo / "contracts" / "mcp" / "pin.json", key="vendoredEntries")
    return _composite_digest(entries)


def _management_context_manifest_digest(repo: Path, reasons: list[str]) -> str:
    pin_errors = _verify_contract_pin(repo, "management")
    if pin_errors:
        reasons.extend(f"management_pin:{e}" for e in pin_errors)
    entries = _pin_entries(repo / "contracts" / "management" / "pin.json", key="ownedEntries")
    return _composite_digest(entries)


def _pii_policy_digest(repo: Path, reasons: list[str]) -> str:
    pin_errors = _verify_contract_pin(repo, "pii")
    if pin_errors:
        reasons.extend(f"pii_pin:{e}" for e in pin_errors)
    pin = json.loads((repo / "contracts" / "pii" / "pin.json").read_text(encoding="utf-8"))
    digest = pin["groundTruth"]["canonicalDigest"]
    if digest != PINNED_SP60_POLICY_DIGEST:
        reasons.append(
            f"pii_policy_digest_mismatch:pin={digest}:pinned_constant={PINNED_SP60_POLICY_DIGEST}"
        )
    return digest


def _schema_set_digest(repo: Path, reasons: list[str]) -> str:
    mcp_entries = _pin_entries(repo / "contracts" / "mcp" / "pin.json", key="vendoredEntries")
    mgmt_entries = _pin_entries(repo / "contracts" / "management" / "pin.json", key="ownedEntries")
    pii_entries = _pin_entries(repo / "contracts" / "pii" / "pin.json", key="vendoredEntries")
    namespaced = (
        [(f"mcp/{p}", d) for p, d in mcp_entries]
        + [(f"management/{p}", d) for p, d in mgmt_entries]
        + [(f"pii/{p}", d) for p, d in pii_entries]
    )
    if not namespaced:
        reasons.append("schema_set_empty")
    return _composite_digest(namespaced)


def _severance_fixture_digest(repo: Path) -> str | None:
    path = repo / "contracts" / "releases" / PROFILE_ID / "severance-fixtures.json"
    if not path.is_file():
        return None
    return _sha256_file(path)


def _language_neutral_consumer_kit_digest(repo: Path) -> str | None:
    release_dir = repo / "contracts" / "releases" / PROFILE_ID
    kit_dir = release_dir / "consumer-kit-go"
    lock_schema = release_dir / "schemas" / "v1" / "release-lock.schema.json"
    if not kit_dir.is_dir() or not lock_schema.is_file() or lock_schema.is_symlink():
        return None
    generated = {
        kit_dir / "management-readonly-v1-consumer-kit",
        kit_dir / "management-readonly-v1-consumer-kit.exe",
    }
    files = [
        path
        for path in kit_dir.rglob("*")
        if path not in generated and (not path.is_dir() or path.is_symlink())
    ]
    if not files or any(path.is_symlink() or not path.is_file() for path in files):
        return None
    return _sha256_tree([*files, lock_schema], root=repo)


def _rollback_release_digest(repo: Path, reasons: list[str]) -> str:
    path = repo / "contracts" / "releases" / PROFILE_ID / "rollback.json"
    if not path.is_file():
        reasons.append("rollback_json_missing")
        return "none:rollback_json_missing"
    rollback = json.loads(path.read_text(encoding="utf-8"))
    prior = rollback.get("prior_release_digest")
    if prior is None:
        return "none:first-candidate"
    if not isinstance(prior, str) or not prior:
        reasons.append("rollback_release_digest_malformed")
        return "none:rollback_release_digest_malformed"
    return prior


def build_release_lock(*, repo: Path = REPO_ROOT) -> tuple[dict[str, Any], VerificationResult]:
    """Build and self-validate the ``OmniscienceManagementReadOnlyRelease`` object.

    Never raises on a missing/dirty/mismatched input -- every failure mode
    becomes a RED reason folded into the returned ``VerificationResult``, and
    the release lock is still returned (with honest placeholder digests where
    a required input was unusable) so callers can inspect exactly what was
    wrong.
    """
    reasons: list[str] = []

    git_commit = _current_commit(repo)
    if git_commit is None:
        reasons.append("git_commit_unavailable")
        git_commit = "0" * 40

    scope_clean = _scoped_worktree_is_clean(repo, SCOPED_PATHS)
    if scope_clean is None:
        reasons.append("git_status_unavailable")
        scope_clean = False
    if not scope_clean:
        reasons.append("scoped_worktree_dirty")

    image_digest = _image_digest(repo)
    source_input_digest = _source_input_digest(repo)
    if source_input_digest is None:
        reasons.append("source_input_digest_unavailable")
        source_input_digest = "sha256:" + "0" * 64
    chart_digest = _chart_digest(repo)
    configuration_digest = _configuration_digest(repo)
    mcp_manifest_digest = _mcp_manifest_digest(repo, reasons)
    management_context_manifest_digest = _management_context_manifest_digest(repo, reasons)
    pii_policy_digest = _pii_policy_digest(repo, reasons)
    schema_set_digest = _schema_set_digest(repo, reasons)

    severance_fixture_digest = _severance_fixture_digest(repo)
    if severance_fixture_digest is None:
        reasons.append("severance_fixture_kit_missing")
        severance_fixture_digest = "sha256:" + "0" * 64

    consumer_kit_digest = _language_neutral_consumer_kit_digest(repo)
    if consumer_kit_digest is None:
        reasons.append("language_neutral_consumer_kit_missing")
        consumer_kit_digest = "sha256:" + "0" * 64

    rollback_release_digest = _rollback_release_digest(repo, reasons)

    release_lock: dict[str, Any] = {
        "profile_id": PROFILE_ID,
        "profile_revision": PROFILE_REVISION,
        "git_commit": git_commit,
        "git_worktree_scope_clean": bool(scope_clean),
        "image_digest": image_digest,
        "source_input_digest": source_input_digest,
        "chart_digest": chart_digest,
        "configuration_digest": configuration_digest,
        "mcp_manifest_digest": mcp_manifest_digest,
        "management_context_manifest_digest": management_context_manifest_digest,
        "schema_set_digest": schema_set_digest,
        "pii_policy_revision": PII_POLICY_REVISION,
        "pii_policy_digest": pii_policy_digest,
        "pw0_receipt_refs": [
            "tests/release/management_readonly/test_pw0_seeded_leak.py",
            "tests/test_pii_wall_fail_closed.py",
        ],
        "auth_token_profile": AUTH_TOKEN_PROFILE,
        "dependency_profile": {
            "sp10_mcp_digest": mcp_manifest_digest,
            "sp60_policy_digest": pii_policy_digest,
            "sp61_pw0_ref": "contracts/pii/pin.json",
            "sp81_manifest_digest": management_context_manifest_digest,
            "omnius_status": "not_deployed",
        },
        "readiness_contract": {
            "health_path": "/health",
            "ready_path": "/ready",
            "metrics_path": "/metrics",
        },
        "severance_fixture_digest": severance_fixture_digest,
        "language_neutral_consumer_kit_digest": consumer_kit_digest,
        "evidence_refs": [
            "tests/release/management_readonly/",
            "tests/test_management_readonly_release_lock.py",
            "contracts/releases/management-readonly-v1/evidence/",
        ],
        "rollback_release_digest": rollback_release_digest,
        "pending": {
            "image_registry_digest": (
                "ci-artifact://synchronized-platform/task-sp-86-management-readonly-release/"
                " (pending real docker build + push; see release README"
                " 'External / pending gates')"
            ),
        },
    }

    shape_errors = _validate_release_lock_shape(release_lock)
    reasons.extend(shape_errors)

    status = "RED" if reasons else "GREEN"
    release_lock["status"] = status
    release_lock["reasons"] = reasons
    result = _red(*reasons) if reasons else _green()
    return release_lock, result


_RELEASE_LOCK_REQUIRED_FIELDS: tuple[str, ...] = (
    "profile_id",
    "profile_revision",
    "git_commit",
    "git_worktree_scope_clean",
    "image_digest",
    "source_input_digest",
    "chart_digest",
    "configuration_digest",
    "mcp_manifest_digest",
    "management_context_manifest_digest",
    "schema_set_digest",
    "pii_policy_revision",
    "pii_policy_digest",
    "pw0_receipt_refs",
    "auth_token_profile",
    "dependency_profile",
    "readiness_contract",
    "severance_fixture_digest",
    "language_neutral_consumer_kit_digest",
    "evidence_refs",
    "rollback_release_digest",
    "pending",
)


def _validate_release_lock_shape(release_lock: dict[str, Any]) -> list[str]:
    """Structural self-check against docs/specs/task-sp-86-*.md 'Required
    output' -- every named field must be present and non-empty. This is a
    plain-Python check (same style as ``qualify_backup_restore.py``'s
    ``_MANIFEST_REQUIRED_FIELDS``), not a full JSON-Schema pass; the formal
    shape lives in
    ``contracts/releases/management-readonly-v1/schemas/v1/release-lock.schema.json``
    for the Go consumer kit and any future external validator.
    """
    errors: list[str] = []
    for field in _RELEASE_LOCK_REQUIRED_FIELDS:
        if field not in release_lock or release_lock[field] in (None, "", [], {}):
            errors.append(f"release_lock_field_absent:{field}")
    if release_lock.get("profile_id") != PROFILE_ID:
        errors.append("release_lock_profile_id_mismatch")
    if release_lock.get("auth_token_profile") != AUTH_TOKEN_PROFILE:
        errors.append("release_lock_auth_token_profile_mismatch")
    dependency_profile = release_lock.get("dependency_profile", {})
    if dependency_profile.get("omnius_status") != "not_deployed":
        errors.append("release_lock_omnius_status_not_deployed_required")
    return errors


# ---------------------------------------------------------------------------
# AC-SP86-4: read-only consumer severance receipt verifier
# ---------------------------------------------------------------------------


def _load_severance_fixtures(repo: Path) -> dict[str, dict[str, Any]]:
    path = repo / "contracts" / "releases" / PROFILE_ID / "severance-fixtures.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {row["id"]: row for row in payload["fixtures"]}


def verify_severance_receipt(
    *, receipt: dict[str, Any] | None, repo: Path = REPO_ROOT
) -> VerificationResult:
    """Read-only verifier for a consumer's SP-86 severance receipt.

    Mirrors ``scripts/check_consumer_severance.py``'s shape: the receipt must
    cover every fixture id exactly once and every observed decision must be
    ``fallback_required`` -- a receipt claiming ``healthy``/``empty``/
    ``cached_current`` for any loss/token-expiry/freshness-expiry/
    contract-skew fixture is RED, never accepted as a partial pass.
    """
    if receipt is None:
        return _red("receipt_absent")
    if not isinstance(receipt, dict):
        return _red("receipt_malformed:not_an_object")

    fixtures = _load_severance_fixtures(repo)
    results = receipt.get("results")
    if not isinstance(results, list):
        return _red("receipt_malformed:results_not_list")

    observed: dict[str, str] = {}
    for row in results:
        if not isinstance(row, dict) or "id" not in row or "decision" not in row:
            return _red("receipt_malformed:result_row_shape")
        fixture_id = row["id"]
        decision = row["decision"]
        if not isinstance(fixture_id, str) or not fixture_id.strip():
            return _red("receipt_malformed:result_id_not_nonempty_string")
        if not isinstance(decision, str) or not decision.strip():
            return _red("receipt_malformed:decision_not_nonempty_string")
        if fixture_id in observed:
            return _red(f"receipt_malformed:duplicate_result_id:{fixture_id}")
        observed[fixture_id] = decision

    expected_ids = set(fixtures)
    observed_ids = set(observed)
    reasons: list[str] = []
    missing = expected_ids - observed_ids
    unexpected = observed_ids - expected_ids
    if missing:
        reasons.append(f"receipt_partial:missing={sorted(missing)}")
    if unexpected:
        reasons.append(f"receipt_partial:unexpected={sorted(unexpected)}")
    if reasons:
        return _red(*reasons)

    for fixture_id, fixture in fixtures.items():
        if observed[fixture_id] != fixture["expected_decision"]:
            reasons.append(
                f"decision_mismatch:{fixture_id}:expected={fixture['expected_decision']}:"
                f"observed={observed[fixture_id]!r}"
            )
    if reasons:
        return _red(*reasons)
    return _green()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _write_evidence(repo: Path, release_lock: dict[str, Any]) -> Path:
    evidence_dir = repo / "contracts" / "releases" / PROFILE_ID / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    target = evidence_dir / f"release-lock.{release_lock['git_commit']}.json"
    canonical = json.dumps(release_lock, indent=2, sort_keys=True) + "\n"
    target.write_text(canonical, encoding="utf-8")
    return target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument(
        "--write", action="store_true", help="Persist the release lock under evidence/."
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress the JSON report on stdout.")
    args = parser.parse_args(argv)

    release_lock, result = build_release_lock(repo=args.repo)

    if not args.quiet:
        print(json.dumps(release_lock, indent=2, sort_keys=True))

    if args.write:
        target = _write_evidence(args.repo, release_lock)
        print(f"evidence written: {target}", file=sys.stderr)

    if result.is_green:
        print("qualify-management-readonly-release: GREEN", file=sys.stderr)
        return 0

    print("qualify-management-readonly-release: RED (decision-return)", file=sys.stderr)
    for reason in result.reasons:
        print(f"- {reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
