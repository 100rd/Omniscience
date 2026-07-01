#!/usr/bin/env python3
"""CI guard: fail when a real secret env file is tracked by git.

The README instructs users to write POSTGRES_PASSWORD / OMNISCIENCE_SECRET_KEY
into ``.env``, so a tracked ``.env`` near-guarantees a credential leak
(CWE-312/538). This script flags any git-tracked ``.env`` family file while
allowing templates (``.env.example`` / ``.env.sample`` / ``.env.template``).

Usage: python scripts/check_no_tracked_secrets.py  (run from a git repo)
Exits 0 when clean, 1 when a secret file is tracked.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterable
from pathlib import PurePosixPath

TEMPLATE_SUFFIXES = (".example", ".sample", ".template")


def is_secret_file(path: str) -> bool:
    name = PurePosixPath(path).name
    if name == ".env":
        return True
    return name.startswith(".env.") and not name.endswith(TEMPLATE_SUFFIXES)


def find_tracked_secret_files(paths: Iterable[str]) -> list[str]:
    return [path for path in paths if is_secret_file(path)]


def main() -> int:
    result = subprocess.run(  # noqa: S603, S607 - fixed argv, git comes from PATH
        ["git", "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    )
    tracked = [path for path in result.stdout.split("\0") if path]
    offenders = find_tracked_secret_files(tracked)
    if offenders:
        print("ERROR: secret env files are tracked in git:", file=sys.stderr)
        for path in offenders:
            print(f"  {path}", file=sys.stderr)
        print(
            "Remove them (git rm --cached <file>), rotate any leaked secrets, "
            "and keep real values only in an untracked .env.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
