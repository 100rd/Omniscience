#!/usr/bin/env python3
"""Qualify the Omniscience owner fragment for management-readonly-local-v1.

task-sp-95-management-readonly-local-runtime (ADR-0023). This is the local
counterpart of ``scripts/qualify_management_readonly_release.py`` (SP-86): it
re-derives the fragment's artifact/configuration/behaviour digests and emits an
immutable ``OmniscienceLocalRuntimeReceipt`` (AC-SP95-7).

Discipline mirrored verbatim from SP-86:
  * SHA-256 over raw file bytes / sorted ``path:digest`` composite rows (never
    canonical-JSON-of-object for digests);
  * scoped Git-cleanliness with a ``SCOPED_PATHS`` tuple and
    ``--porcelain=v1 --untracked-files=all`` (so the ~hundreds of unrelated
    graphify-out/ dirty entries never leak in);
  * a frozen decision-return that folds every failure into ``reasons`` and sets
    ``status = "RED" if reasons else "GREEN"`` -- it never raises and never
    fabricates GREEN;
  * honest placeholders ("0"*40, "sha256:"+"0"*64) on unusable inputs;
  * a shape self-check against a required-field tuple + invariant constants;
  * evidence written content-addressed by ``git_commit`` as canonical
    ``sort_keys``/``indent=2`` JSON, append-only.

The consumed SP-86 release lock records only ``pending.image_registry_digest``
(no live OCI digest yet), so IMAGE-mode reproducibility is honestly pending and
a dirty scoped worktree is RED before commit -- exactly as SP-86's own lock is.
This receipt is Omniscience owner-local development evidence: it is not Portal
evidence, platform qualification, production activation, or HA proof.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

PROFILE_ID = "management-readonly-local-v1"
PROFILE_REVISION = "1.0.0"
BASE_PROFILE_ID = "management-readonly-v1"
BASE_PROFILE_REVISION = "1.0.0"

#: The exact SP-86 release lock this fragment consumes (content-addressed by the
#: SP-86 commit). Missing/unpinnable => RED with a named reason (never faked).
SP86_RELEASE_LOCK = (
    REPO_ROOT
    / "contracts"
    / "releases"
    / "management-readonly-v1"
    / "evidence"
    / "release-lock.c733a0d445c090187461814263be7a07d20b7af3.json"
)

SERVICE_CONTRACT = (
    REPO_ROOT / "contracts" / "releases" / "management-readonly-local-v1" / "service-contract.json"
)
FRAGMENT_DIR = REPO_ROOT / "deploy" / "compose" / "management-readonly-local"
COMPOSE_BASE = FRAGMENT_DIR / "compose.yaml"
COMPOSE_SOURCE = FRAGMENT_DIR / "compose.source.yaml"
DOCKERFILE = REPO_ROOT / "Dockerfile"
APP_PY = REPO_ROOT / "apps" / "server" / "src" / "omniscience_server" / "app.py"
HEALTH_PY = REPO_ROOT / "apps" / "server" / "src" / "omniscience_server" / "routes" / "health.py"

#: Independently enumerated rendered-model inventory (owner-prefixed; OML-1/2).
#: Each name is cross-checked against the compose bytes -- a declared resource
#: that is absent from the fragment is RED, not silently digested.
SERVICE_INVENTORY = (
    "omniscience-admin",
    "omniscience-api",
    "omniscience-migrate",
    "omniscience-nats",
    "omniscience-neo4j",
    "omniscience-postgres",
    "omniscience-qdrant",
)
NETWORK_INVENTORY = ("omniscience-private", "omniscience-readnet")
VOLUME_INVENTORY = (
    "omniscience-natsdata",
    "omniscience-neo4jdata",
    "omniscience-neo4jlogs",
    "omniscience-pgdata",
    "omniscience-qdrantdata",
)
#: The sole host binding: the admin UI on a configurable loopback port. No
#: store/broker/API is host-published (AC-SP95-6).
HOST_BINDING_INVENTORY = ("127.0.0.1:admin->80",)

LOCAL_MODEL_IDENTITY = "sentence-transformers/all-MiniLM-L6-v2"

#: Paths this task may write. Cleanliness is scoped to these so the qualifier is
#: not tripped by unrelated dirty entries (graphify-out/, helm/, ...).
SCOPED_PATHS = (
    "Dockerfile",
    ".dockerignore",
    "deploy/compose/management-readonly-local",
    "apps/server/src/omniscience_server/app.py",
    "apps/server/src/omniscience_server/routes/health.py",
    "apps/server/src/omniscience_server/management",
    "apps/server/src/omniscience_server/privacy",
    "packages/core/src/omniscience_core/management",
    "packages/core/src/omniscience_core/privacy",
    "contracts/releases/management-readonly-local-v1",
    "scripts/qualify_management_readonly_local.py",
    "tests/release/management_readonly_local",
    "tests/test_management_readonly_local_runtime.py",
    "docs/runbooks/management-readonly-local.md",
    ".github/workflows/management-readonly-local.yml",
)

_ZERO_COMMIT = "0" * 40
_ZERO_DIGEST = "sha256:" + "0" * 64

_RECEIPT_REQUIRED_FIELDS = (
    "profile_id",
    "profile_revision",
    "base_profile_id",
    "base_profile_revision",
    "sp86_release_digest",
    "sp86_release_git_commit",
    "sp86_release_status",
    "mode",
    "reproducible",
    "qualification_status",
    "git_commit",
    "git_worktree_scope_clean",
    "image_content_digest",
    "configuration_digest",
    "service_contract_digest",
    "fragment_digest",
    "rendered_fragment_digest",
    "service_inventory_digest",
    "network_inventory_digest",
    "volume_inventory_digest",
    "host_binding_inventory_digest",
    "local_model_digest",
    "dependency_closure_digest",
    "readiness_digest",
    "evidence_refs",
    "pending",
    "availability_class",
    "ha_qualified",
    "activation_authority",
)


@dataclass(frozen=True)
class VerificationResult:
    """Decision-return; never raised. ``reasons`` is empty iff GREEN."""

    status: str  # "GREEN" | "RED"
    reasons: tuple[str, ...] = ()


def _green() -> VerificationResult:
    return VerificationResult(status="GREEN")


def _red(*reasons: str) -> VerificationResult:
    return VerificationResult(status="RED", reasons=tuple(reasons))


# --- digest helpers (mirror SP-86 exactly) ---------------------------------


def _sha256_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _sha256_tree(paths: Iterable[Path], *, root: Path) -> str:
    """Composite digest: sha256 of sorted ``relpath:hexdigest\\n`` rows."""
    rows: list[str] = []
    for path in paths:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        rows.append(f"{relative}:{digest}\n")
    canonical = "".join(sorted(rows)).encode("utf-8")
    return _sha256_bytes(canonical)


def _sha256_names(names: Iterable[str]) -> str:
    """Composite digest over an ordered set of identifier strings."""
    canonical = "".join(f"{name}\n" for name in sorted(names)).encode("utf-8")
    return _sha256_bytes(canonical)


# --- git helpers (mirror SP-86 scoped-cleanliness discipline) --------------


class GitUnavailableError(RuntimeError):
    pass


def _git(repo: Path, *args: str) -> str:
    git_exe = shutil.which("git")
    if git_exe is None:
        raise GitUnavailableError("git not on PATH")
    try:
        proc = subprocess.run(  # noqa: S603
            [git_exe, "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:  # pragma: no cover
        raise GitUnavailableError(str(exc)) from exc
    return proc.stdout


def _current_commit(repo: Path) -> str | None:
    try:
        return _git(repo, "rev-parse", "HEAD").strip().lower()
    except GitUnavailableError:
        return None


def _scoped_worktree_is_clean(repo: Path, scoped_paths: tuple[str, ...]) -> bool | None:
    """True/False, or None when Git is unavailable (never silently clean)."""
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


# --- component digests / cross-checks --------------------------------------


def _sp86_release(reasons: list[str]) -> tuple[str, str, str]:
    """Return (release_digest, sp86_git_commit, sp86_status).

    Consumes the exact SP-86 release lock. Missing/malformed => RED with a named
    reason and honest placeholders (never fabricated).
    """
    if not SP86_RELEASE_LOCK.exists():
        reasons.append("sp86_release_lock_missing")
        return _ZERO_DIGEST, _ZERO_COMMIT, "unknown"
    try:
        lock = json.loads(SP86_RELEASE_LOCK.read_text(encoding="utf-8"))
    except ValueError:
        reasons.append("sp86_release_lock_unreadable")
        return _ZERO_DIGEST, _ZERO_COMMIT, "unknown"
    digest = _sha256_file(SP86_RELEASE_LOCK)
    commit = str(lock.get("git_commit", _ZERO_COMMIT))
    status = str(lock.get("status", "unknown"))
    if lock.get("profile_id") != BASE_PROFILE_ID:
        reasons.append("sp86_release_profile_mismatch")
    # The consumed release has no live OCI registry digest yet -- record it,
    # do not fabricate one.
    if isinstance(lock.get("pending"), dict) and lock["pending"].get("image_registry_digest"):
        reasons.append("sp86_image_registry_digest_pending")
    return digest, commit, status


def _configuration_digest() -> str:
    surface = [p for p in (APP_PY, HEALTH_PY) if p.exists()]
    return _sha256_tree(surface, root=REPO_ROOT)


def _fragment_digest() -> str:
    files = sorted(p for p in FRAGMENT_DIR.rglob("*") if p.is_file())
    return _sha256_tree(files, root=REPO_ROOT)


def _rendered_fragment_digest(rendered_config: Path | None, reasons: list[str]) -> str:
    """Digest of the actual ``docker compose config`` output when supplied.

    Rendering requires Docker, so it is captured during the live smoke and
    passed via ``--rendered-config``. Absent => pending (not fabricated).
    """
    if rendered_config is None:
        reasons.append("rendered_config_not_supplied")
        return _ZERO_DIGEST
    if not rendered_config.exists():
        reasons.append("rendered_config_missing")
        return _ZERO_DIGEST
    return _sha256_file(rendered_config)


def _inventory_digests(reasons: list[str]) -> dict[str, str]:
    """Digest each independently-enumerated inventory and cross-check the
    compose bytes actually declare each named resource."""
    compose_bytes = COMPOSE_BASE.read_bytes() if COMPOSE_BASE.exists() else b""
    compose_text = compose_bytes.decode("utf-8", errors="replace")
    if not compose_bytes:
        reasons.append("compose_base_missing")
    for name in (*SERVICE_INVENTORY, *NETWORK_INVENTORY, *VOLUME_INVENTORY):
        if name not in compose_text:
            reasons.append(f"inventory_absent_from_fragment:{name}")
    # No generic / mock / omnius / sibling resource may appear.
    for forbidden in ("mock-", "omnius", "selected-owner", "barbarossa-", "portal-"):
        if forbidden in compose_text:
            reasons.append(f"forbidden_resource_token:{forbidden}")
    return {
        "service_inventory_digest": _sha256_names(SERVICE_INVENTORY),
        "network_inventory_digest": _sha256_names(NETWORK_INVENTORY),
        "volume_inventory_digest": _sha256_names(VOLUME_INVENTORY),
        "host_binding_inventory_digest": _sha256_names(HOST_BINDING_INVENTORY),
    }


def _readiness_and_dependency_digests(reasons: list[str]) -> tuple[str, str]:
    if not SERVICE_CONTRACT.exists():
        reasons.append("service_contract_missing")
        return _ZERO_DIGEST, _ZERO_DIGEST
    try:
        contract = json.loads(SERVICE_CONTRACT.read_text(encoding="utf-8"))
    except ValueError:
        reasons.append("service_contract_unreadable")
        return _ZERO_DIGEST, _ZERO_DIGEST
    readiness = contract.get("readiness_contract", {})
    dep_closure = readiness.get("dependency_closure", [])
    typed_reasons = readiness.get("typed_unready_reasons", [])
    if set(dep_closure) != {"postgres", "nats", "neo4j", "qdrant"}:
        reasons.append("dependency_closure_incomplete")
    dependency_digest = _sha256_names(dep_closure) if dep_closure else _ZERO_DIGEST
    readiness_digest = _sha256_names([*readiness.get("policy_init_closure", []), *typed_reasons])
    return dependency_digest, readiness_digest


def _image_content_digest(reasons: list[str]) -> str:
    if not DOCKERFILE.exists():
        reasons.append("dockerfile_missing")
        return _ZERO_DIGEST
    return _sha256_file(DOCKERFILE)


def _service_contract_digest(reasons: list[str]) -> str:
    if not SERVICE_CONTRACT.exists():
        reasons.append("service_contract_missing")
        return _ZERO_DIGEST
    return _sha256_file(SERVICE_CONTRACT)


def _local_model_digest() -> str:
    return _sha256_names([LOCAL_MODEL_IDENTITY])


# --- receipt assembly ------------------------------------------------------


def _validate_receipt_shape(receipt: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in _RECEIPT_REQUIRED_FIELDS:
        if field not in receipt:
            errors.append(f"receipt_field_absent:{field}")
            continue
        value = receipt[field]
        if value in (None, "", [], {}) and field not in ("git_worktree_scope_clean",):
            errors.append(f"receipt_field_empty:{field}")
    if receipt.get("profile_id") != PROFILE_ID:
        errors.append("receipt_profile_id_mismatch")
    if receipt.get("availability_class") != "development-single-host":
        errors.append("receipt_availability_class_invalid")
    if receipt.get("ha_qualified") is not False:
        errors.append("receipt_ha_qualified_must_be_false")
    if receipt.get("activation_authority") != "none":
        errors.append("receipt_activation_authority_must_be_none")
    return errors


def build_receipt(
    *,
    mode: str = "source",
    rendered_config: Path | None = None,
    repo: Path = REPO_ROOT,
) -> tuple[dict[str, Any], VerificationResult]:
    """Assemble the receipt + decision. Never raises; folds all failures into
    ``reasons`` and RED. Digest fields are byte-exactly reproducible across
    calls against the same tree (the exact-rollback prerequisite)."""
    reasons: list[str] = []

    git_commit = _current_commit(repo)
    if git_commit is None:
        reasons.append("git_commit_unavailable")
        git_commit = _ZERO_COMMIT
    scope_clean = _scoped_worktree_is_clean(repo, SCOPED_PATHS)
    if scope_clean is None:
        reasons.append("git_status_unavailable")
        scope_clean = False
    elif not scope_clean:
        reasons.append("scoped_worktree_dirty")

    sp86_digest, sp86_commit, sp86_status = _sp86_release(reasons)
    inventory = _inventory_digests(reasons)
    dependency_digest, readiness_digest = _readiness_and_dependency_digests(reasons)
    rendered_digest = _rendered_fragment_digest(rendered_config, reasons)

    # A dirty scoped tree (or any missing input) can never claim reproducibility.
    # Image mode additionally needs a live SP-86 OCI digest, which is pending.
    reproducible = bool(scope_clean) and not reasons and mode == "image"
    qualification_status = "reproducible" if reproducible else "development-only"

    pending: dict[str, str] = {
        "sp86_image_registry_digest": (
            "unpublished; SP-86 release lock records only "
            "pending.image_registry_digest (no live OCI digest yet)"
        ),
        "image_mode_reproducible": (
            "requires a published SP-86 OCI digest (OMNISCIENCE_API_IMAGE=...@sha256:...)"
        ),
        "amd64_resource_measurement": (
            "this qualification host is arm64; amd64 measurements are pending a "
            "second-arch run (cannot run both arches on one host)"
        ),
    }
    if rendered_config is None:
        pending["rendered_fragment_digest"] = (
            "supply --rendered-config <docker compose config output> to bind the rendered model"
        )

    receipt: dict[str, Any] = {
        "profile_id": PROFILE_ID,
        "profile_revision": PROFILE_REVISION,
        "base_profile_id": BASE_PROFILE_ID,
        "base_profile_revision": BASE_PROFILE_REVISION,
        "sp86_release_digest": sp86_digest,
        "sp86_release_git_commit": sp86_commit,
        "sp86_release_status": sp86_status,
        "mode": mode,
        "reproducible": reproducible,
        "qualification_status": qualification_status,
        "git_commit": git_commit,
        "git_worktree_scope_clean": bool(scope_clean),
        "image_content_digest": _image_content_digest(reasons),
        "configuration_digest": _configuration_digest(),
        "service_contract_digest": _service_contract_digest(reasons),
        "fragment_digest": _fragment_digest(),
        "rendered_fragment_digest": rendered_digest,
        "service_inventory_digest": inventory["service_inventory_digest"],
        "network_inventory_digest": inventory["network_inventory_digest"],
        "volume_inventory_digest": inventory["volume_inventory_digest"],
        "host_binding_inventory_digest": inventory["host_binding_inventory_digest"],
        "local_model_digest": _local_model_digest(),
        "dependency_closure_digest": dependency_digest,
        "readiness_digest": readiness_digest,
        "evidence_refs": [
            "tests/release/management_readonly_local/",
            "tests/release/management_readonly_local/live_mcp.py",
            "tests/release/management_readonly_local/test_live_mcp_capture.py",
            "tests/test_management_readonly_local_runtime.py",
            "docs/runbooks/management-readonly-local.md",
            "contracts/releases/management-readonly-local-v1/service-contract.json",
        ],
        "pending": pending,
        "availability_class": "development-single-host",
        "ha_qualified": False,
        "activation_authority": "none",
    }

    reasons.extend(_validate_receipt_shape(receipt))

    status = "RED" if reasons else "GREEN"
    receipt["status"] = status
    receipt["reasons"] = reasons
    result = _red(*reasons) if reasons else _green()
    return receipt, result


def _write_evidence(repo: Path, receipt: dict[str, Any]) -> Path:
    evidence_dir = repo / "contracts" / "releases" / "management-readonly-local-v1" / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    commit = receipt.get("git_commit", _ZERO_COMMIT)
    out = evidence_dir / f"local-runtime-receipt.{commit}.json"
    out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Qualify management-readonly-local-v1 (SP-95).")
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument("--mode", choices=("source", "image"), default="source")
    parser.add_argument(
        "--rendered-config",
        type=Path,
        default=None,
        help="Path to captured `docker compose config` output to bind the rendered model.",
    )
    parser.add_argument(
        "--write", action="store_true", help="Persist the receipt under evidence/."
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    receipt, result = build_receipt(
        mode=args.mode, rendered_config=args.rendered_config, repo=args.repo
    )
    if args.write:
        out = _write_evidence(args.repo, receipt)
        if not args.quiet:
            print(f"wrote {out}")
    if not args.quiet:
        print(json.dumps(receipt, indent=2, sort_keys=True))
    if result.status == "RED":
        print(f"RED ({args.mode} mode)")
        for reason in result.reasons:
            print(f"- {reason}")
        return 1
    print(f"GREEN ({args.mode} mode)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
