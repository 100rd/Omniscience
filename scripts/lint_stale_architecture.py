#!/usr/bin/env python3
import re
import sys
from pathlib import Path

BANNED_PATTERNS = [
    r"(?i)pgvector was removed",
    r"(?i)#105 cutover",
    r"(?i)sql-side graph storage is now deprecated",
    r"(?i)unwired after #105",
    r"(?i)legacy_retrieval_service_unwired"
]

def check_file(path: Path) -> list[tuple[int, str]]:
    errors = []
    try:
        with open(path, encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                for pattern in BANNED_PATTERNS:
                    if re.search(pattern, line):
                        errors.append((i, line.strip()))
    except Exception:
        pass
    return errors

def main():
    if len(sys.argv) < 2:
        sys.exit(0)
    has_errors = False
    for arg in sys.argv[1:]:
        path = Path(arg)
        if not path.is_file():
            continue
        errors = check_file(path)
        for line_num, line_content in errors:
            print(f"{path}:{line_num}: Stale architecture comment/string detected: {line_content}")
            has_errors = True
    if has_errors:
        sys.exit(1)

if __name__ == "__main__":
    main()
