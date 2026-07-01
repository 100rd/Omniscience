"""Tests for the pinned install flow (review action point: replace the
unpinned ``curl -fsSL .../install.sh | bash`` with a checksum/version-pinned
flow).

Contract under test:

Every public surface that advertises the one-line install — README.md, the
catalog submission doc, the PulseMCP manifest, the Cline marketplace entry,
and the installer's own usage header — currently pipes a script fetched from
the mutable ``main`` ref straight into ``bash``. That contradicts the
project's self-hosted / perimeter-security positioning: the script content
can silently change between what was reviewed and what a user executes.

Acceptance criteria encoded here:

1. No install surface pipes a remotely fetched ``install.sh`` directly into
   a shell (``curl ... | bash`` and friends are gone).
2. Every URL that fetches ``install.sh`` is pinned to an immutable,
   version-tag ref (``vX.Y.Z``) — not ``main``/``master``/``HEAD``. Both
   ``raw.githubusercontent.com/<org>/<repo>/<tag>/...`` and GitHub release
   asset URLs (``.../releases/download/<tag>/...``) are accepted spellings.
3. Each install surface documents a SHA-256 verification step
   (``sha256sum``/``shasum -a 256`` with ``-c``/``--check`` or an explicit
   expected digest) between download and execution.
4. The expected checksum is recorded in-repo — either inline in an install
   surface or as a ``.mcp/install.sh.sha256`` sidecar — and every advertised
   digest matches the actual SHA-256 of ``.mcp/install.sh``, so a release
   that edits the installer must bump the published checksums.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
INSTALLER = REPO_ROOT / ".mcp" / "install.sh"
CHECKSUM_SIDECAR = REPO_ROOT / ".mcp" / "install.sh.sha256"

# Files that advertise the one-line install command to end users.
INSTALL_SURFACES = [
    "README.md",
    "docs/mcp-catalog-submission.md",
    ".mcp/pulsemcp.json",
    ".mcp/cline-marketplace.json",
]

# Everything swept for pipe-to-shell regressions: the install surfaces plus
# the installer's own usage header and any other docs/manifests.
def _sweep_files() -> list[str]:
    files = {*INSTALL_SURFACES, ".mcp/install.sh"}
    files.update(
        str(p.relative_to(REPO_ROOT)) for p in (REPO_ROOT / "docs").rglob("*.md")
    )
    files.update(
        str(p.relative_to(REPO_ROOT)) for p in (REPO_ROOT / ".mcp").glob("*.json")
    )
    return sorted(files)


# ``.../install.sh | bash`` (also sh/zsh/dash, optional sudo). Matches both
# plain markdown and JSON-escaped one-liners, where the command sits on a
# single (logical) line.
PIPE_TO_SHELL = re.compile(r"install\.sh[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b")

# Any full URL that fetches install.sh.
INSTALLER_URL = re.compile(r"https?://[^\s\"'`<>()\[\]]*?install\.sh\b")

# Immutable version-tag ref, e.g. v0.5.0 / v1.2.3-rc1.
VERSION_TAG = re.compile(r"^v\d+(?:\.\d+){1,2}(?:[-.][0-9A-Za-z.]+)?$")

# SHA-256 verification step in documented commands.
SHA256_TOOL = re.compile(r"(?:sha256sum|shasum\s+(?:-a\s*256|--algorithm[= ]\s*256))")
SHA256_CHECK_FLAG = re.compile(r"(?:--check\b|(?<=\s)-c\b)")
HEX_DIGEST = re.compile(r"\b[0-9a-fA-F]{64}\b")


def _read(rel_path: str) -> str:
    return (REPO_ROOT / rel_path).read_text(encoding="utf-8")


def _installer_ref(url: str) -> str | None:
    """Extract the git ref an install.sh URL is pinned to, if recognizable."""
    m = re.search(r"raw\.githubusercontent\.com/[^/]+/[^/]+/([^/]+)/", url)
    if m:
        return m.group(1)
    m = re.search(r"github\.com/[^/]+/[^/]+/releases/download/([^/]+)/", url)
    if m:
        return m.group(1)
    return None


@pytest.mark.parametrize("rel_path", _sweep_files())
def test_no_remote_installer_piped_to_shell(rel_path: str) -> None:
    """No doc or manifest may pipe a fetched install.sh straight into a shell."""
    text = _read(rel_path)
    hits = PIPE_TO_SHELL.findall(text)
    assert not hits, (
        f"{rel_path} pipes install.sh directly into a shell ({hits!r}); "
        "document a download → verify checksum → execute flow instead"
    )


@pytest.mark.parametrize(
    "rel_path", sorted({*INSTALL_SURFACES, ".mcp/install.sh"})
)
def test_installer_urls_pinned_to_immutable_ref(rel_path: str) -> None:
    """Every install.sh fetch URL must be pinned to a version tag, not a branch."""
    text = _read(rel_path)
    for url in INSTALLER_URL.findall(text):
        ref = _installer_ref(url)
        assert ref is not None, (
            f"{rel_path}: unrecognized install.sh URL {url!r}; expected a "
            "raw.githubusercontent.com/<org>/<repo>/<tag>/... or "
            "github.com/<org>/<repo>/releases/download/<tag>/... form"
        )
        assert ref.lower() not in {"main", "master", "head"}, (
            f"{rel_path}: install.sh is fetched from mutable ref {ref!r} "
            f"({url}); pin a released version tag instead"
        )
        assert VERSION_TAG.match(ref), (
            f"{rel_path}: install.sh URL ref {ref!r} is not a version tag "
            f"(expected e.g. v0.5.0): {url}"
        )


@pytest.mark.parametrize("rel_path", INSTALL_SURFACES)
def test_install_surfaces_document_checksum_verification(rel_path: str) -> None:
    """Each advertised install flow must verify the script's SHA-256 before running it."""
    text = _read(rel_path)
    assert SHA256_TOOL.search(text), (
        f"{rel_path}: install instructions lack a SHA-256 verification step "
        "(sha256sum / shasum -a 256) between download and execution"
    )
    assert SHA256_CHECK_FLAG.search(text) or HEX_DIGEST.search(text), (
        f"{rel_path}: SHA-256 tool is mentioned but nothing is actually "
        "verified — use `-c`/`--check` against a checksum file or compare "
        "an explicit expected digest"
    )


def _advertised_digests() -> dict[str, set[str]]:
    """Collect every 64-hex digest advertised near install.sh instructions."""
    digests: dict[str, set[str]] = {}
    for rel_path in INSTALL_SURFACES:
        text = _read(rel_path)
        # Every install surface exists solely to document the install flow,
        # so any 64-hex digest it contains is an advertised installer checksum.
        found = {d.lower() for d in HEX_DIGEST.findall(text)}
        if found:
            digests[rel_path] = found
    if CHECKSUM_SIDECAR.exists():
        sidecar = {
            d.lower() for d in HEX_DIGEST.findall(
                CHECKSUM_SIDECAR.read_text(encoding="utf-8")
            )
        }
        assert sidecar, f"{CHECKSUM_SIDECAR.name} exists but contains no SHA-256 digest"
        digests[str(CHECKSUM_SIDECAR.relative_to(REPO_ROOT))] = sidecar
    return digests


def test_checksum_is_recorded_in_repo() -> None:
    """The expected installer checksum must live in-repo so releases bump it."""
    assert _advertised_digests(), (
        "no installer SHA-256 is recorded anywhere: add an inline digest to "
        "an install surface or ship a .mcp/install.sh.sha256 sidecar"
    )


def test_advertised_digests_match_installer() -> None:
    """Every published digest must equal the actual SHA-256 of .mcp/install.sh."""
    actual = hashlib.sha256(INSTALLER.read_bytes()).hexdigest()
    mismatches = {
        source: sorted(found)
        for source, found in _advertised_digests().items()
        if found != {actual}
    }
    assert not mismatches, (
        f"advertised install.sh checksums are stale (actual: {actual}): "
        f"{mismatches!r} — regenerate the published digests for this release"
    )
