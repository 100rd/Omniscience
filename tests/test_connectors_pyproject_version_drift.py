"""Regression test for the k8s-agentic deprecation-schedule comment in
``packages/connectors/pyproject.toml`` (issue #168, ADR-0011).

The deprecation-schedule comment block documents which release line is
"(current line)" so that customers reading the file know which row of the
v0.3 / v0.4 / v0.5 schedule applies today. That label is a plain code
comment, so nothing keeps it in sync with the package's actual
``[project].version`` field when a release cuts a new line -- this test
guards against the two drifting apart again.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

CONNECTORS_PYPROJECT = (
    Path(__file__).resolve().parents[1] / "packages" / "connectors" / "pyproject.toml"
)

CURRENT_LINE_RE = re.compile(r"#\s*v(\d+\.\d+)\s*\(current line\)")


def _actual_major_minor() -> str:
    data = tomllib.loads(CONNECTORS_PYPROJECT.read_text())
    version = data["project"]["version"]
    major, minor, *_ = version.split(".")
    return f"{major}.{minor}"


def test_deprecation_schedule_current_line_matches_package_version() -> None:
    """The comment's "(current line)" version must match [project].version.

    The schedule comment in packages/connectors/pyproject.toml labels one
    release line as "(current line)" (e.g. "v0.3 (current line)"). That
    label must always name the same major.minor as the package's actual
    ``version`` field -- otherwise the documented deprecation timeline
    misleads customers about which behaviour (warn vs. enforce vs. remove)
    applies to the version they have installed.
    """
    text = CONNECTORS_PYPROJECT.read_text()

    matches = CURRENT_LINE_RE.findall(text)
    assert len(matches) == 1, (
        "Expected exactly one '(current line)' marker in the deprecation "
        f"schedule comment; found {matches!r}"
    )

    documented_current_line = matches[0]
    actual = _actual_major_minor()
    assert documented_current_line == actual, (
        "Deprecation schedule comment claims the current line is "
        f"v{documented_current_line}, but [project].version resolves to "
        f"v{actual} -- update the comment to match the released version."
    )
