#!/usr/bin/env python3
import re
import sys
from pathlib import Path
import ast

BANNED_PATTERNS = [
    r"(?i)pgvector was removed",
    r"(?i)#105 cutover",
    r"(?i)sql-side graph storage is now deprecated",
    r"(?i)unwired after #105",
    r"(?i)legacy_retrieval_service_unwired"
]

def get_available_adrs(docs_dir: Path) -> set[str]:
    adrs = set()
    decisions_dir = docs_dir / "decisions"
    if decisions_dir.exists():
        for file in decisions_dir.glob("*.md"):
            match = re.match(r"^(\d+)-.*\.md$", file.name)
            if match:
                adrs.add(match.group(1))
    return adrs

def get_available_tests(tests_dir: Path) -> set[str]:
    test_ids = set()
    if tests_dir.exists():
        for path in tests_dir.rglob("*.py"):
            try:
                content = path.read_text(encoding="utf-8")
                tree = ast.parse(content)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        if node.name.startswith("test_") or node.name.startswith("Test"):
                            test_ids.add(node.name)
            except Exception:
                pass
    return test_ids

def check_file(path: Path, available_adrs: set[str], available_tests: set[str]) -> list[tuple[int, str]]:
    errors = []
    try:
        with open(path, encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                # Check banned patterns
                for pattern in BANNED_PATTERNS:
                    if re.search(pattern, line):
                        errors.append((i, f"Stale architecture comment/string detected: {line.strip()}"))
                
                # Check ADRs
                for adr_match in re.finditer(r"(?i)ADR-(\d{4}|\d+)", line):
                    adr_num = adr_match.group(1).zfill(4)
                    if adr_num not in available_adrs:
                        errors.append((i, f"doc-drift: References unknown or deleted ADR-{adr_num}"))
                
                # Check test-ids
                for test_match in re.finditer(r"(?i)test-id:\s*([a-zA-Z0-9_]+)", line):
                    test_id = test_match.group(1)
                    if test_id not in available_tests:
                        errors.append((i, f"contract-drift: References unknown test-id '{test_id}'"))
    except Exception:
        pass
    return errors

def main():
    if len(sys.argv) < 2:
        sys.exit(0)
        
    root_dir = Path(__file__).resolve().parent.parent
    docs_dir = root_dir / "docs"
    tests_dir = root_dir / "tests"
    
    available_adrs = get_available_adrs(docs_dir)
    available_tests = get_available_tests(tests_dir)
    
    has_errors = False
    for arg in sys.argv[1:]:
        path = Path(arg)
        if not path.is_file():
            continue
        errors = check_file(path, available_adrs, available_tests)
        for line_num, msg in errors:
            print(f"{path}:{line_num}: {msg}")
            has_errors = True
            
    if has_errors:
        sys.exit(1)

if __name__ == "__main__":
    main()
