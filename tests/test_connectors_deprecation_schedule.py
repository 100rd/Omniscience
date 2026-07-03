"""Guards the `k8s-agentic` deprecation schedule (ADR-0011) against version drift.

``packages/connectors/pyproject.toml`` carries a hand-maintained comment
pinning the deprecation schedule for the legacy ``k8s-agentic`` connector.
The comment marks one release line as ``(current line)`` and states the
``OMNISCIENCE_K8S_AGENTIC_ALLOWED`` feature-flag default for that line. If
the package's ``version`` field drifts out of sync with the line the comment
calls "current", a reader has no reliable way to know which default
(``true`` in v0.3, ``false`` in v0.4) actually applies today -- see
consilium round-1 P1.

These tests assert the ``version`` field and the comment's ``(current
line)`` marker always name the same release line, and that the comment
documents a ``default true`` for whichever line is current (matching the
actual runtime default asserted in ``tests/test_ingestion_dedup.py::
TestConfigFromEnv::test_agentic_allowed_default_true_when_unset``).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
CONNECTORS_PYPROJECT = REPO_ROOT / "packages" / "connectors" / "pyproject.toml"


def _connectors_pyproject_text() -> str:
    return CONNECTORS_PYPROJECT.read_text(encoding="utf-8")


def _connectors_version() -> str:
    config = tomllib.loads(_connectors_pyproject_text())
    return config["project"]["version"]


def _version_line(version: str) -> str:
    """``"0.2.0"`` -> ``"v0.2"``; the release-line granularity the ADR-0011
    schedule comment reasons about."""
    major, minor, *_ = version.split(".")
    return f"v{major}.{minor}"


def _deprecation_schedule_comment() -> str:
    """The ADR-0011 schedule comment, normalized to a single-spaced string.

    The comment is hand-wrapped across multiple ``#``-prefixed lines, so a
    phrase like ``default true in v0.3`` can have the version number pushed
    onto the next line (``...in\\n#     v0.3...``). Stripping the ``#``
    leaders and joining on single spaces lets regexes match phrases without
    caring where the source happened to wrap.
    """
    text = _connectors_pyproject_text()
    start = text.index("Deprecation schedule")
    end = text.index("dependencies = [")
    raw = text[start:end]
    lines = [line.lstrip("#").strip() for line in raw.splitlines()]
    return " ".join(line for line in lines if line)


class TestConnectorsVersionMatchesDeprecationScheduleComment:
    def test_current_line_marker_matches_package_version(self) -> None:
        """The comment's ``vX.Y  (current line)`` marker must name the same
        release line as the package's actual ``version`` field.

        Today ``version = "0.2.0"`` (line v0.2) while the comment marks
        ``v0.3  (current line)`` -- a direct contradiction this test pins
        down. Whether the fix bumps the version to 0.3.0 or corrects the
        comment's line label, this test only requires the two agree.
        """
        version = _connectors_version()
        expected_line = _version_line(version)

        comment = _deprecation_schedule_comment()
        match = re.search(r"(v\d+\.\d+)\s*\(current line\)", comment)
        assert match is not None, (
            "expected a 'vX.Y  (current line)' marker in the deprecation "
            "schedule comment at the top of packages/connectors/pyproject.toml"
        )
        marked_line = match.group(1)

        assert marked_line == expected_line, (
            f"deprecation-schedule comment marks {marked_line} as the "
            f"'(current line)' but packages/connectors/pyproject.toml has "
            f"version={version!r} ({expected_line}). These must agree -- "
            f"ADR-0011 pins a different OMNISCIENCE_K8S_AGENTIC_ALLOWED "
            f"default per line (true in v0.3, false in v0.4), so a mismatch "
            f"leaves the flag's real default ambiguous."
        )

    def test_current_line_documents_agentic_allowed_default_true(self) -> None:
        """Whichever line the comment marks as current, it must explicitly
        state ``OMNISCIENCE_K8S_AGENTIC_ALLOWED`` defaults to ``true`` for
        that line -- matching the actual runtime default (unset env var =>
        ``True``, see ``ingestion/dedup.py::config_from_env`` and
        ``ingestion/worker.py::_AGENTIC_ALLOWED_ENV``).
        """
        version = _connectors_version()
        current_line = _version_line(version)

        comment = _deprecation_schedule_comment()
        pattern = re.compile(
            r"default\s*``?true``?\s*in\s*" + re.escape(current_line),
            re.IGNORECASE,
        )
        assert pattern.search(comment), (
            f"deprecation-schedule comment does not state a "
            f"'default true in {current_line}' for OMNISCIENCE_K8S_AGENTIC_ALLOWED "
            f"-- the comment must spell out the flag's real default for "
            f"whichever release line is actually current, so it isn't left "
            f"ambiguous by a stale line label."
        )
