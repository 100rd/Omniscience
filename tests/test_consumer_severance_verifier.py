"""AC-SEV-5 conformance tests — the consumer-severance verifier is read-only.

Invariant: `scripts/check_consumer_severance.py` accepts only an explicit
consumer revision string and a receipt file path; it never edits, creates,
commits, or pushes anything, in this repository or any other, on any code
path (GREEN or RED). Absent, dirty, mutable, or partial evidence returns an
explicit RED decision return — never an uncaught exception, never a
fabricated GREEN.

Test categories
----------------
1. Read-only proof — directory fingerprint identical before/after, on both
   the GREEN and every RED path.
2. Negative RED paths: absent receipt, dirty/mutable/malformed revision,
   malformed JSON, stale fixture digest, partial coverage, decision
   mismatch, revision mismatch.
3. Positive GREEN path: a receipt built from this producer's own current
   fixture matrix passes.
4. CLI (`main`) exit-code and stdout/stderr contract.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from collections.abc import Iterable
from pathlib import Path

import pytest

from tests.conformance.consumer_severance.fixtures import (
    all_ids,
    expected_decisions,
    fixture_matrix_sha256,
)

ROOT = Path(__file__).resolve().parents[1]
VERIFIER_PATH = ROOT / "scripts" / "check_consumer_severance.py"
_SPEC = importlib.util.spec_from_file_location("check_consumer_severance", VERIFIER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
verifier = importlib.util.module_from_spec(_SPEC)
# Register before exec: `verify`'s frozen dataclass needs `cls.__module__` to
# resolve via sys.modules during class creation (Python 3.12+ dataclasses).
sys.modules[_SPEC.name] = verifier
_SPEC.loader.exec_module(verifier)

VALID_REVISION = "9" * 40
OTHER_VALID_REVISION = "8" * 40
WATCHED_DIRS: tuple[Path, ...] = (
    ROOT / "scripts",
    ROOT / "tests" / "conformance" / "consumer_severance",
)


def _fingerprint(paths: Iterable[Path]) -> dict[str, str]:
    fingerprint: dict[str, str] = {}
    for base in paths:
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file():
                fingerprint[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return fingerprint


def _valid_receipt(*, revision: str = VALID_REVISION) -> dict[str, object]:
    decisions = expected_decisions()
    return {
        "consumer_revision": revision,
        "fixture_matrix_sha256": fixture_matrix_sha256(),
        "command": "uv run pytest tests/severance/test_omniscience_fitness_matrix.py -q",
        "results": [
            {"id": fixture_id, "decision": decision} for fixture_id, decision in decisions.items()
        ],
    }


def _write_receipt(tmp_path: Path, payload: object, *, name: str = "receipt.json") -> Path:
    receipt_path = tmp_path / name
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")
    return receipt_path


# ---------------------------------------------------------------------------
# 1. Read-only proof
# ---------------------------------------------------------------------------


def test_green_path_never_writes_or_mutates_any_file(tmp_path: Path) -> None:
    receipt_path = _write_receipt(tmp_path, _valid_receipt())
    watched = (*WATCHED_DIRS, tmp_path)
    before = _fingerprint(watched)

    result = verifier.verify(consumer_revision=VALID_REVISION, receipt_path=receipt_path)

    after = _fingerprint(watched)
    assert result.is_green
    assert before == after


@pytest.mark.parametrize(
    "build_case",
    [
        lambda tmp_path: (None, None),
        lambda tmp_path: (VALID_REVISION, None),
        lambda tmp_path: (VALID_REVISION + "-dirty", _write_receipt(tmp_path, _valid_receipt())),
        lambda tmp_path: (
            VALID_REVISION,
            _write_receipt(tmp_path, {**_valid_receipt(), "results": []}),
        ),
        lambda tmp_path: (VALID_REVISION, _write_receipt(tmp_path, "not-json-object")),
    ],
    ids=["all-absent", "receipt-absent", "dirty-revision", "partial-results", "malformed-shape"],
)
def test_red_paths_never_write_or_mutate_any_file(tmp_path: Path, build_case: object) -> None:
    consumer_revision, receipt_path = build_case(tmp_path)  # type: ignore[operator]
    watched = (*WATCHED_DIRS, tmp_path)
    before = _fingerprint(watched)

    result = verifier.verify(consumer_revision=consumer_revision, receipt_path=receipt_path)

    after = _fingerprint(watched)
    assert result.status == "RED"
    assert before == after


# ---------------------------------------------------------------------------
# 2. Negative RED paths
# ---------------------------------------------------------------------------


def test_absent_receipt_is_red_decision_return_not_a_crash() -> None:
    result = verifier.verify(consumer_revision=VALID_REVISION, receipt_path=None)
    assert result.status == "RED"
    assert result.reasons == ("receipt_absent",)


def test_nonexistent_receipt_path_is_red(tmp_path: Path) -> None:
    result = verifier.verify(
        consumer_revision=VALID_REVISION,
        receipt_path=tmp_path / "does-not-exist.json",
    )
    assert result.status == "RED"
    assert result.reasons[0].startswith("receipt_absent")


@pytest.mark.parametrize(
    "bad_revision",
    [
        None,
        "",
        "9" * 39,  # too short
        "9" * 41,  # too long
        "9" * 40 + "-dirty",  # mutable working tree marker
        "g" * 40,  # not hex
        "9" * 39 + "G",  # not hex (uppercase disallowed)
    ],
)
def test_non_immutable_revision_is_red(tmp_path: Path, bad_revision: str | None) -> None:
    receipt_path = _write_receipt(tmp_path, _valid_receipt())
    result = verifier.verify(consumer_revision=bad_revision, receipt_path=receipt_path)
    assert result.status == "RED"
    assert result.reasons[0].startswith("revision_not_immutable")


def test_malformed_json_receipt_is_red(tmp_path: Path) -> None:
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text("{not valid json", encoding="utf-8")
    result = verifier.verify(consumer_revision=VALID_REVISION, receipt_path=receipt_path)
    assert result.status == "RED"
    assert result.reasons[0].startswith("receipt_malformed")


def test_receipt_that_is_not_a_json_object_is_red(tmp_path: Path) -> None:
    receipt_path = _write_receipt(tmp_path, [1, 2, 3])
    result = verifier.verify(consumer_revision=VALID_REVISION, receipt_path=receipt_path)
    assert result.status == "RED"
    assert result.reasons == ("receipt_malformed:not_an_object",)


def test_revision_mismatch_between_arg_and_receipt_is_red(tmp_path: Path) -> None:
    receipt_path = _write_receipt(tmp_path, _valid_receipt(revision=OTHER_VALID_REVISION))
    result = verifier.verify(consumer_revision=VALID_REVISION, receipt_path=receipt_path)
    assert result.status == "RED"
    assert result.reasons[0].startswith("revision_mismatch")


def test_stale_fixture_matrix_digest_is_red(tmp_path: Path) -> None:
    receipt = _valid_receipt()
    receipt["fixture_matrix_sha256"] = "0" * 64
    receipt_path = _write_receipt(tmp_path, receipt)
    result = verifier.verify(consumer_revision=VALID_REVISION, receipt_path=receipt_path)
    assert result.status == "RED"
    assert result.reasons[0].startswith("fixture_digest_mismatch")


def test_missing_command_field_is_red(tmp_path: Path) -> None:
    receipt = _valid_receipt()
    del receipt["command"]
    receipt_path = _write_receipt(tmp_path, receipt)
    result = verifier.verify(consumer_revision=VALID_REVISION, receipt_path=receipt_path)
    assert result.status == "RED"
    assert result.reasons == ("receipt_command_missing",)


def test_partial_coverage_receipt_is_red(tmp_path: Path) -> None:
    receipt = _valid_receipt()
    receipt["results"] = receipt["results"][:-1]  # type: ignore[index]
    receipt_path = _write_receipt(tmp_path, receipt)
    result = verifier.verify(consumer_revision=VALID_REVISION, receipt_path=receipt_path)
    assert result.status == "RED"
    assert any(reason.startswith("receipt_partial:missing") for reason in result.reasons)


def test_extra_unexpected_ids_receipt_is_red(tmp_path: Path) -> None:
    receipt = _valid_receipt()
    receipt["results"] = [
        *receipt["results"],  # type: ignore[list-item]
        {"id": "not-a-real-fixture", "decision": "use"},
    ]
    receipt_path = _write_receipt(tmp_path, receipt)
    result = verifier.verify(consumer_revision=VALID_REVISION, receipt_path=receipt_path)
    assert result.status == "RED"
    assert any(reason.startswith("receipt_partial:unexpected") for reason in result.reasons)


def test_single_decision_mismatch_is_red(tmp_path: Path) -> None:
    receipt = _valid_receipt()
    results = copy.deepcopy(receipt["results"])
    results[0]["decision"] = "use-a-wrong-decision"  # type: ignore[index]
    receipt["results"] = results
    receipt_path = _write_receipt(tmp_path, receipt)
    result = verifier.verify(consumer_revision=VALID_REVISION, receipt_path=receipt_path)
    assert result.status == "RED"
    assert any(reason.startswith("decision_mismatch:") for reason in result.reasons)


def test_malformed_result_row_is_red(tmp_path: Path) -> None:
    receipt = _valid_receipt()
    receipt["results"] = [{"id": "missing-decision-key"}]
    receipt_path = _write_receipt(tmp_path, receipt)
    result = verifier.verify(consumer_revision=VALID_REVISION, receipt_path=receipt_path)
    assert result.status == "RED"
    assert result.reasons == ("receipt_malformed:result_row_shape",)


def test_non_string_result_id_is_red_not_a_crash(tmp_path: Path) -> None:
    """A JSON array/object `id` deserializes to an unhashable Python type.

    Using it directly as a dict key would raise an uncaught
    `TypeError: unhashable type`, not a RED decision-return.
    """
    receipt = _valid_receipt()
    receipt["results"] = [{"id": ["not", "a", "string"], "decision": "use"}]
    receipt_path = _write_receipt(tmp_path, receipt)
    result = verifier.verify(consumer_revision=VALID_REVISION, receipt_path=receipt_path)
    assert result.status == "RED"
    assert result.reasons == ("receipt_malformed:result_row_shape",)


def test_empty_string_result_id_is_red(tmp_path: Path) -> None:
    receipt = _valid_receipt()
    receipt["results"] = [{"id": "", "decision": "use"}]
    receipt_path = _write_receipt(tmp_path, receipt)
    result = verifier.verify(consumer_revision=VALID_REVISION, receipt_path=receipt_path)
    assert result.status == "RED"
    assert result.reasons == ("receipt_malformed:result_row_shape",)


def test_non_utf8_receipt_bytes_is_red_not_a_crash(tmp_path: Path) -> None:
    """Invalid UTF-8 bytes raise `UnicodeDecodeError` (a `ValueError`

    subclass), not `OSError` — must be caught alongside `OSError` in
    `_load_receipt`, or this crashes instead of returning RED.
    """
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_bytes(b'{"consumer_revision": "\xff\xfe invalid utf8 bytes here"}')
    result = verifier.verify(consumer_revision=VALID_REVISION, receipt_path=receipt_path)
    assert result.status == "RED"
    assert result.reasons[0].startswith("receipt_not_utf8")


def test_oversized_receipt_is_red_not_a_crash(tmp_path: Path) -> None:
    receipt_path = tmp_path / "receipt.json"
    padding = "x" * (verifier._MAX_RECEIPT_BYTES + 1)
    receipt_path.write_text(padding, encoding="utf-8")
    result = verifier.verify(consumer_revision=VALID_REVISION, receipt_path=receipt_path)
    assert result.status == "RED"
    assert result.reasons[0].startswith("receipt_too_large")


def test_cli_main_prints_red_marker_not_traceback_on_unhashable_id(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    receipt = _valid_receipt()
    receipt["results"] = [{"id": ["not", "a", "string"], "decision": "use"}]
    receipt_path = _write_receipt(tmp_path, receipt)
    exit_code = verifier.main(
        ["--consumer-revision", VALID_REVISION, "--receipt", str(receipt_path)]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "RED" in captured.err
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out


def test_cli_main_prints_red_marker_not_traceback_on_non_utf8_receipt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_bytes(b'{"consumer_revision": "\xff\xfe invalid utf8 bytes here"}')
    exit_code = verifier.main(
        ["--consumer-revision", VALID_REVISION, "--receipt", str(receipt_path)]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "RED" in captured.err
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out


# ---------------------------------------------------------------------------
# 3. Positive GREEN path
# ---------------------------------------------------------------------------


def test_full_valid_receipt_against_producer_fixture_matrix_is_green(tmp_path: Path) -> None:
    receipt_path = _write_receipt(tmp_path, _valid_receipt())
    result = verifier.verify(consumer_revision=VALID_REVISION, receipt_path=receipt_path)
    assert result.status == "GREEN"
    assert result.reasons == ()
    receipt_ids = {entry["id"] for entry in _valid_receipt()["results"]}  # type: ignore[union-attr]
    assert receipt_ids == all_ids()


# ---------------------------------------------------------------------------
# 4. CLI contract
# ---------------------------------------------------------------------------


def test_main_returns_zero_and_prints_green_on_success(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    receipt_path = _write_receipt(tmp_path, _valid_receipt())
    exit_code = verifier.main(
        ["--consumer-revision", VALID_REVISION, "--receipt", str(receipt_path)]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "GREEN" in captured.out


def test_main_returns_one_and_does_not_crash_on_no_args() -> None:
    exit_code = verifier.main([])
    assert exit_code == 1


def test_main_returns_one_on_absent_receipt(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = verifier.main(["--consumer-revision", VALID_REVISION])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "RED" in captured.err
    assert "receipt_absent" in captured.err
