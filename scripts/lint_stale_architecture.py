#!/usr/bin/env python3
import ast
import re
import sys
from pathlib import Path

BANNED_PATTERNS = [
    r"(?i)pgvector was removed",
    r"(?i)#105 cutover",
    r"(?i)sql-side graph storage is now deprecated",
    r"(?i)unwired after #105",
    r"(?i)legacy_retrieval_service_unwired",
]

#: A line containing this inline marker is exempt from ALL stale-architecture
#: checks. Use it to sanction a reviewed, intentional reference.
ALLOW_MARKER = "stale-arch-allow"

#: Test-tree files whose module docstrings intentionally narrate removed
#: architecture (historical scaffolding notes). Exempt from BANNED_PATTERNS
#: only — ADR/test-id drift checks still apply to them.
BANNED_PATTERN_EXEMPT = {
    "tests/test_embedded_inference.py",
    "tests/test_store_contract.py",
    "tests/support/graph_store_proxy.py",
}

#: The linter's own test file carries deliberate negative fixtures (e.g.
#: "pgvector was removed", ADR-9999). It is fully excluded from scanning.
SELF_EXCLUDE = {"tests/test_lint_stale_architecture.py"}


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
                    if isinstance(
                        node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                    ) and (node.name.startswith("test_") or node.name.startswith("Test")):
                        test_ids.add(node.name)
            except Exception:  # noqa: S110 - best-effort AST walk, failures are benign
                pass
    return test_ids


def check_docstrings_ast(content: str, path: Path) -> list[tuple[int, str]]:
    errors = []
    try:
        tree = ast.parse(content)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                docstring = ast.get_docstring(node)
                if not docstring:
                    continue

                documented_args = set()
                in_args_section = False
                for line in docstring.split("\n"):
                    line = line.strip()
                    if line.startswith("Args:"):
                        in_args_section = True
                        continue
                    elif (in_args_section and line == "") or (
                        in_args_section
                        and (
                            line.startswith("Returns:")
                            or line.startswith("Raises:")
                            or line.startswith("Yields:")
                        )
                    ):
                        in_args_section = False
                        continue

                    if in_args_section:
                        match = re.match(r"^(\w+)(?:\s*\([^)]+\))?\s*:", line)
                        if match:
                            documented_args.add(match.group(1))

                if documented_args:
                    actual_args = set()
                    for arg in getattr(node.args, "args", []):
                        actual_args.add(arg.arg)
                    for arg in getattr(node.args, "kwonlyargs", []):
                        actual_args.add(arg.arg)
                    if getattr(node.args, "vararg", None):
                        actual_args.add(node.args.vararg.arg)
                    if getattr(node.args, "kwarg", None):
                        actual_args.add(node.args.kwarg.arg)
                    for arg in getattr(node.args, "posonlyargs", []):
                        actual_args.add(arg.arg)

                    actual_args.discard("self")
                    actual_args.discard("cls")

                    drifted = documented_args - actual_args
                    if drifted:
                        errors.append(
                            (
                                node.lineno,
                                "doc-drift: Docstring for '"
                                f"{node.name}' documents args not in signature: "
                                f"{', '.join(sorted(drifted))}",
                            )
                        )
    except Exception:  # noqa: S110 - best-effort AST parse, failures are benign
        pass
    return errors


def check_file(
    path: Path,
    available_adrs: set[str],
    available_tests: set[str],
    *,
    skip_banned: bool = False,
) -> list[tuple[int, str]]:
    errors = []
    try:
        content = path.read_text(encoding="utf-8")

        if path.suffix == ".py":
            errors.extend(check_docstrings_ast(content, path))

        lines = content.splitlines()
        for i, line in enumerate(lines, 1):
            if ALLOW_MARKER in line:
                continue
            if not skip_banned:
                for pattern in BANNED_PATTERNS:
                    if re.search(pattern, line):
                        errors.append(
                            (i, f"Stale architecture comment/string detected: {line.strip()}")
                        )

            for adr_match in re.finditer(r"(?i)ADR-(\d{4}|\d+)", line):
                adr_num = adr_match.group(1).zfill(4)
                if adr_num not in available_adrs:
                    errors.append((i, f"doc-drift: References unknown or deleted ADR-{adr_num}"))

            for test_match in re.finditer(r"(?i)test-id:\s*([a-zA-Z0-9_]+)", line):
                test_id = test_match.group(1)
                if test_id not in available_tests:
                    errors.append((i, f"contract-drift: References unknown test-id '{test_id}'"))

    except Exception:  # noqa: S110 - best-effort file scan, failures are benign
        pass
    return sorted(errors, key=lambda x: x[0])


def check_adr_file(path: Path, root_dir: Path) -> list[tuple[int, str]]:
    errors = []
    try:
        content = path.read_text(encoding="utf-8")
        lines = content.splitlines()
        for i, line in enumerate(lines, 1):
            if ALLOW_MARKER in line:
                continue
            for match in re.finditer(r"`([a-zA-Z0-9_/-]+\.py)`", line):
                py_path_str = match.group(1)
                if "/" in py_path_str:
                    target_path = root_dir / py_path_str
                    if not target_path.exists():
                        errors.append(
                            (i, f"doc-drift: ADR references missing file '{py_path_str}'")
                        )
    except Exception:  # noqa: S110 - best-effort ADR scan, failures are benign
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

    paths_to_check = []
    for arg in sys.argv[1:]:
        p = Path(arg)
        if p.is_file():
            paths_to_check.append(p)
        elif p.is_dir():
            paths_to_check.extend(p.rglob("*.py"))
            paths_to_check.extend(p.rglob("*.md"))

    paths_to_check = list(set(paths_to_check))
    paths_to_check = [p for p in paths_to_check if p.as_posix() not in SELF_EXCLUDE]

    for path in paths_to_check:
        skip_banned = path.as_posix() in BANNED_PATTERN_EXEMPT
        errors = check_file(path, available_adrs, available_tests, skip_banned=skip_banned)

        if path.suffix == ".md" and "docs/decisions" in path.as_posix():
            errors.extend(check_adr_file(path, root_dir))
            errors.sort(key=lambda x: x[0])

        for line_num, msg in errors:
            print(f"{path}:{line_num}: {msg}")
            has_errors = True

    if has_errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
