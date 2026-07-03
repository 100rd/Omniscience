"""Repo-identity consistency tests (review action point P1).

README.md, operator/go.mod, and the TypeScript SDK's package.json each named a
different GitHub identity for this project (100rd/Omniscience,
100rd/omniscience, omniscience-ai/omniscience). That split breaks `go get`
module resolution (the module path must match the real repo path/case) and
npm provenance (package.json `repository`/`homepage` must point at the repo
that actually published it).

The canonical identity is github.com/100rd/Omniscience — it matches the
project's git remote, README.md, PROJECT.md, the published container images,
and the MCP registry manifests. These tests pin go.mod and the TypeScript
SDK's package metadata to that same identity so all three sources agree.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

CANONICAL_OWNER = "100rd"
CANONICAL_REPO = "Omniscience"
CANONICAL_GITHUB_URL = f"https://github.com/{CANONICAL_OWNER}/{CANONICAL_REPO}"
CANONICAL_GO_MODULE = f"github.com/{CANONICAL_OWNER}/{CANONICAL_REPO}/operator"

MODULE_RE = re.compile(r"^module\s+(\S+)", re.MULTILINE)
GO_IMPORT_RE = re.compile(r'"(github\.com/[^"]*[Oo]mniscience/operator[^"]*)"')


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def _go_module_path() -> str:
    go_mod = _read("operator/go.mod")
    match = MODULE_RE.search(go_mod)
    assert match, "operator/go.mod must declare a `module` directive"
    return match.group(1)


def test_readme_references_canonical_github_identity() -> None:
    readme = _read("README.md")
    assert CANONICAL_GITHUB_URL in readme


def test_go_module_path_matches_canonical_identity() -> None:
    module_path = _go_module_path()
    assert module_path == CANONICAL_GO_MODULE, (
        f"operator/go.mod declares module {module_path!r}, which does not match "
        f"the canonical repo identity {CANONICAL_GO_MODULE!r} used by README.md "
        "and the project's git remote. A mismatched module path/case breaks "
        "`go get` resolution for consumers of the operator module."
    )


def test_go_internal_imports_use_the_declared_module_path() -> None:
    module_path = _go_module_path()
    mismatched: list[tuple[str, str]] = []
    for go_file in (REPO_ROOT / "operator").rglob("*.go"):
        text = go_file.read_text(encoding="utf-8")
        for imported in GO_IMPORT_RE.findall(text):
            if not imported.startswith(module_path):
                mismatched.append((str(go_file.relative_to(REPO_ROOT)), imported))
    assert not mismatched, (
        "internal operator imports must use the exact module path declared in "
        f"go.mod ({module_path!r}); found mismatches: {mismatched[:10]}"
    )


def test_typescript_sdk_package_json_uses_canonical_identity() -> None:
    package = json.loads(_read("sdks/typescript/package.json"))
    assert package.get("homepage") == CANONICAL_GITHUB_URL, (
        f"sdks/typescript/package.json homepage {package.get('homepage')!r} must "
        f"point at the canonical repo identity {CANONICAL_GITHUB_URL!r}, not a "
        "different GitHub org — a mismatch breaks npm provenance."
    )
    repository = package.get("repository")
    repository_url = repository.get("url") if isinstance(repository, dict) else repository
    assert repository_url == CANONICAL_GITHUB_URL, (
        f"sdks/typescript/package.json repository.url {repository_url!r} must "
        f"point at the canonical repo identity {CANONICAL_GITHUB_URL!r}, not a "
        "different GitHub org — a mismatch breaks npm provenance."
    )


def test_typescript_sdk_readme_references_canonical_identity() -> None:
    readme = _read("sdks/typescript/README.md")
    assert "omniscience-ai" not in readme, (
        "sdks/typescript/README.md must not reference the stale 'omniscience-ai' "
        "org — it should point at the canonical github.com/100rd/Omniscience repo"
    )
    assert CANONICAL_GITHUB_URL in readme
