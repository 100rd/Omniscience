#!/usr/bin/env python3
"""CI guard: fail when a git-tracked file violates the repo's own .gitignore.

Scratch/cache artifacts (``.hypothesis/``, ``.agents/``, ``.pytest_cache/``,
ad-hoc debug scripts) have landed in version control before, which bloats the
repo and proves .gitignore was not enforced. This script flags every tracked
path that .gitignore says should be ignored (``git ls-files -ci
--exclude-standard``) so a re-committed artifact fails the build.

Usage: python scripts/check_gitignore_enforced.py  (run from a git repo)
Exits 0 when clean, 1 when a tracked file violates .gitignore.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def tracked_ignored_paths(repo_root: Path | str) -> list[str]:
    """Return git-tracked paths that the repo's ignore rules say to ignore."""
    result = subprocess.run(  # noqa: S603, S607 - fixed argv, git comes from PATH
        ["git", "ls-files", "-ci", "--exclude-standard", "-z"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return [path for path in result.stdout.split("\0") if path]


def main() -> int:
    offenders = tracked_ignored_paths(Path.cwd())
    if offenders:
        print("ERROR: git-tracked files violate .gitignore:", file=sys.stderr)
        for path in offenders:
            print(f"  {path}", file=sys.stderr)
        print(
            "They are local scratch/cache state — remove them from the index "
            "(git rm -r --cached <path>) and let .gitignore keep them out.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
