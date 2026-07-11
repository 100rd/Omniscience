#!/usr/bin/env python3
"""Validate ADR ids, capability contracts, and ready task SPECs."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
ADR_DIR = ROOT / "docs" / "decisions"
CAPABILITY_DIR = ROOT / "specs"
TASK_DIR = ROOT / "docs" / "specs"
CODEOWNERS = ROOT / ".github" / "CODEOWNERS"

ADR_FILE = re.compile(r"^(?P<number>\d{4})-[a-z0-9][a-z0-9-]*\.md$")
ADR_HEADER = re.compile(r"^# ADR[- ](?P<number>\d{4})\b", re.MULTILINE)
ADR_STATUS = re.compile(r"\*{0,2}Status\*{0,2}\s*:\s*(?P<status>[A-Za-z-]+)")
ALLOWED_ADR_STATUSES = {"proposed", "accepted", "implemented", "superseded", "deprecated"}
CAPABILITY_FILE = re.compile(r"^SPEC-(?P<id>[A-Z]+)-[a-z0-9][a-z0-9-]*\.md$")
REQ = re.compile(r"\[REQ-(?P<id>[A-Z]+)-(?P<number>\d+)\]")
READY_PROVENANCE = re.compile(
    r"^Readiness:\s*human-approved by (?P<owner>@[A-Za-z0-9-]+) "
    r"on \d{4}-\d{2}-\d{2} under accepted ADR-0019$",
    re.MULTILINE,
)


def _load_frontmatter(path: Path) -> dict[str, Any] | None:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return None
    try:
        _, raw, _ = text.split("---", 2)
        value = yaml.safe_load(raw)
    except (ValueError, yaml.YAMLError) as exc:
        raise ValueError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("frontmatter must be a mapping")
    return value


def validate_adrs(directory: Path = ADR_DIR) -> list[str]:
    errors: list[str] = []
    seen: dict[str, Path] = {}
    for path in sorted(directory.glob("[0-9][0-9][0-9][0-9]-*.md")):
        match = ADR_FILE.fullmatch(path.name)
        if match is None:
            errors.append(f"{path}: invalid ADR filename")
            continue
        number = match.group("number")
        if previous := seen.get(number):
            errors.append(f"{path}: duplicate ADR-{number}; first defined by {previous}")
        seen[number] = path

        text = path.read_text(encoding="utf-8")
        header = ADR_HEADER.search(text)
        if header is None or header.group("number") != number:
            errors.append(f"{path}: H1 must declare ADR-{number}")
        status = ADR_STATUS.search(text)
        if status is None or status.group("status").lower() not in ALLOWED_ADR_STATUSES:
            errors.append(f"{path}: missing or invalid ADR status")
    return errors


def _adr_statuses(directory: Path) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for path in directory.glob("[0-9][0-9][0-9][0-9]-*.md"):
        file_match = ADR_FILE.fullmatch(path.name)
        status_match = ADR_STATUS.search(path.read_text(encoding="utf-8"))
        if file_match is not None and status_match is not None:
            statuses[file_match.group("number")] = status_match.group("status").lower()
    return statuses


def _codeowners_grants_readiness(path: Path, pattern: str, owner: str) -> bool:
    if not path.is_file():
        return False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if fields[0] == pattern and owner in fields[1:]:
            return True
    return False


def validate_capabilities(
    directory: Path = CAPABILITY_DIR,
    *,
    adr_directory: Path = ADR_DIR,
    codeowners_path: Path = CODEOWNERS,
) -> list[str]:
    errors: list[str] = []
    adr_statuses = _adr_statuses(adr_directory)
    for path in sorted(directory.glob("SPEC-*.md")):
        if path.name == "SPEC-INDEX.md":
            continue
        match = CAPABILITY_FILE.fullmatch(path.name)
        if match is None:
            errors.append(f"{path}: invalid capability SPEC filename")
            continue
        spec_id = match.group("id")
        text = path.read_text(encoding="utf-8")
        status_pattern = r"^Status:\s*(draft|ready|implemented|verified|superseded)\b"
        status_match = re.search(status_pattern, text, re.M)
        if status_match is None:
            errors.append(f"{path}: missing or invalid capability status")
        elif status_match.group(1) == "ready":
            provenance = READY_PROVENANCE.search(text)
            if provenance is None:
                errors.append(f"{path}: ready capability has no human readiness provenance")
            else:
                owner = provenance.group("owner")
                if not _codeowners_grants_readiness(codeowners_path, "/specs/", owner):
                    errors.append(f"{path}: readiness owner {owner} does not own /specs/")
            if adr_statuses.get("0019") != "accepted":
                errors.append(f"{path}: ready capability requires accepted ADR-0019")

        matches = list(REQ.finditer(text))
        if not matches:
            errors.append(f"{path}: no REQ-{spec_id}-* requirements")
            continue
        numbers: list[int] = []
        for index, req_match in enumerate(matches):
            req_id = req_match.group("id")
            number = int(req_match.group("number"))
            numbers.append(number)
            if req_id != spec_id:
                errors.append(
                    f"{path}: requirement id REQ-{req_id}-{number} mismatches SPEC-{spec_id}"
                )
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            if "Fallback:" not in text[req_match.start() : end]:
                errors.append(f"{path}: REQ-{spec_id}-{number} has no Fallback")
            if not re.search(rf"\bP-{spec_id}-{number}\b", text):
                errors.append(f"{path}: REQ-{spec_id}-{number} has no matching probe")
        expected = list(range(1, len(numbers) + 1))
        if numbers != expected:
            errors.append(f"{path}: requirement ids must be sequential; got {numbers}")
    return errors


def _contains_tbd(value: Any) -> bool:
    if isinstance(value, str):
        return "TBD" in value.upper()
    if isinstance(value, dict):
        return any(_contains_tbd(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_tbd(item) for item in value)
    return False


def validate_tasks(
    directory: Path = TASK_DIR,
    *,
    codeowners_path: Path = CODEOWNERS,
) -> list[str]:
    errors: list[str] = []
    for path in sorted(directory.glob("*.md")):
        if path.name == "README.md":
            continue
        try:
            data = _load_frontmatter(path)
        except ValueError as exc:
            errors.append(f"{path}: {exc}")
            continue
        if data is None or str(data.get("status", "draft")).lower() != "ready":
            continue

        required = {
            "id",
            "title",
            "source",
            "governingAdrs",
            "capabilitySpecs",
            "sddMode",
            "repo",
            "scope",
            "acceptanceCriteria",
            "rollback",
            "readiness",
        }
        missing = sorted(required - data.keys())
        if missing:
            errors.append(f"{path}: ready task missing fields: {', '.join(missing)}")
        if _contains_tbd(data):
            errors.append(f"{path}: ready task contains TBD")
        if Path(str(data.get("repo", ""))).is_absolute():
            errors.append(f"{path}: ready task repo must not be an absolute workstation path")
        if data.get("sddMode") not in {"quick", "standard", "full"}:
            errors.append(f"{path}: ready task has invalid sddMode")
        readiness = data.get("readiness")
        if not isinstance(readiness, dict) or not {"approvedBy", "approvedAt"}.issubset(readiness):
            errors.append(f"{path}: ready task readiness requires approvedBy and approvedAt")
        else:
            owner = str(readiness["approvedBy"])
            approved_at = str(readiness["approvedAt"])
            if not re.fullmatch(r"@[A-Za-z0-9-]+", owner):
                errors.append(f"{path}: ready task approvedBy must be a GitHub owner")
            elif not _codeowners_grants_readiness(codeowners_path, "/docs/specs/", owner):
                errors.append(f"{path}: readiness owner {owner} does not own /docs/specs/")
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", approved_at):
                errors.append(f"{path}: ready task approvedAt must be YYYY-MM-DD")
        for field in ("governingAdrs", "capabilitySpecs"):
            if not isinstance(data.get(field), list) or not data[field]:
                errors.append(f"{path}: ready task {field} must be a non-empty list")
        criteria = data.get("acceptanceCriteria")
        if not isinstance(criteria, list) or not criteria:
            errors.append(f"{path}: ready task acceptanceCriteria must be a non-empty list")
        else:
            criterion_fields = {"id", "requirement", "probe", "expected", "groundTruth"}
            for index, criterion in enumerate(criteria, start=1):
                if not isinstance(criterion, dict):
                    errors.append(f"{path}: acceptance criterion {index} must be structured")
                elif missing_criterion := sorted(criterion_fields - criterion.keys()):
                    errors.append(
                        f"{path}: acceptance criterion {index} missing: "
                        f"{', '.join(missing_criterion)}"
                    )
        rollback = data.get("rollback")
        if not isinstance(rollback, dict) or not {"kind", "probe"}.issubset(rollback):
            errors.append(f"{path}: ready task rollback requires kind and probe")
    return errors


def validate_all() -> list[str]:
    return [*validate_adrs(), *validate_capabilities(), *validate_tasks()]


def main() -> int:
    errors = validate_all()
    for error in errors:
        print(error)
    if errors:
        print(f"governance validation: {len(errors)} error(s)", file=sys.stderr)
        return 1
    print("governance validation: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
