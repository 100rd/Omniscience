"""Tests for TS SDK package provenance (review action point: fix the
repository URL in sdks/typescript/package.json).

Contract under test:

The project lives at https://github.com/100rd/Omniscience (the canonical
origin referenced throughout README.md and the MCP registry manifests), but
sdks/typescript/package.json declares ``repository`` and ``homepage`` under
the non-existent org ``omniscience-ai/omniscience``. npm surfaces those
fields verbatim on the package page, so published metadata points consumers
at the wrong org.

Acceptance criteria encoded here:

1. ``repository.url`` resolves to the 100rd/Omniscience GitHub repo
   (``git+`` prefix and ``.git`` suffix are accepted npm spellings).
2. ``homepage`` points at the same repo.
3. No trace of the stale ``omniscience-ai`` org remains anywhere in
   package.json.
4. The SDK README — which npm always bundles into the published tarball and
   renders on the package page — does not link to the stale org either.
"""

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SDK_DIR = REPO_ROOT / "sdks" / "typescript"
PACKAGE_JSON = SDK_DIR / "package.json"

CANONICAL_REPO_PATH = "100rd/omniscience"
STALE_ORG = "omniscience-ai"


def _load_package_json() -> dict:
    return json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))


def _github_repo_path(url: str) -> str:
    """Normalize a GitHub URL to its ``org/repo`` path, lower-cased.

    Accepts the npm spellings ``https://github.com/org/repo``,
    ``git+https://github.com/org/repo.git`` and trailing slashes.
    """
    match = re.search(r"github\.com[/:]([^/]+/[^/#?]+)", url)
    assert match, f"not a recognizable GitHub URL: {url!r}"
    path = match.group(1)
    path = path.removesuffix(".git").rstrip("/")
    return path.lower()


def test_repository_url_points_at_canonical_org():
    pkg = _load_package_json()
    repository = pkg.get("repository")
    assert isinstance(repository, dict), (
        "package.json must declare a repository object with type/url"
    )
    assert repository.get("type") == "git"
    url = repository.get("url", "")
    assert _github_repo_path(url) == CANONICAL_REPO_PATH, (
        f"repository.url must point at github.com/{CANONICAL_REPO_PATH}, "
        f"got {url!r}"
    )


def test_homepage_points_at_canonical_org():
    pkg = _load_package_json()
    homepage = pkg.get("homepage", "")
    assert _github_repo_path(homepage) == CANONICAL_REPO_PATH, (
        f"homepage must point at github.com/{CANONICAL_REPO_PATH}, "
        f"got {homepage!r}"
    )


def test_no_stale_org_reference_in_package_json():
    raw = PACKAGE_JSON.read_text(encoding="utf-8")
    assert STALE_ORG not in raw.lower(), (
        f"package.json still references the stale org {STALE_ORG!r}"
    )


def test_no_stale_org_reference_in_sdk_readme():
    readme = SDK_DIR / "README.md"
    assert readme.is_file(), "TS SDK must ship a README.md (npm publishes it)"
    raw = readme.read_text(encoding="utf-8")
    assert STALE_ORG not in raw.lower(), (
        f"sdks/typescript/README.md still links to the stale org "
        f"{STALE_ORG!r}; npm bundles the README into the published tarball"
    )
