"""AC-DR-2 tests: destructive-command approval envelope verification.

Absent, expired, mismatched, or replayed approval must abort before
mutation, each with a field-specific RED reason — see fixture ids
approval-* in test_backup_restore_fixtures.py.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import qualify_backup_restore
from qualify_backup_restore import (
    _ENVELOPE_REQUIRED_FIELDS,
    FileNonceLedger,
    InMemoryNonceLedger,
    NonceLedgerCorruptError,
    compute_command_digest,
    verify_approval_envelope,
)
from qualify_backup_restore import main as cli_main

NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
EXPECTED_COMMAND = "python scripts/rebuild_all_projections.py --yes --restore-from backup-123"
EXPECTED_DIGEST = compute_command_digest(EXPECTED_COMMAND)
EXPECTED_SOURCE_BACKUP_ID = "backup-123"
EXPECTED_TARGET_ACCOUNT = "111122223333"
EXPECTED_TARGET_REGION = "us-east-1"
OBSERVED_ENVIRONMENT_IDENTITY = "recovery-env-alpha"


def _valid_envelope(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "command_digest": EXPECTED_DIGEST,
        "source_backup_id": EXPECTED_SOURCE_BACKUP_ID,
        "target_account": EXPECTED_TARGET_ACCOUNT,
        "target_region": EXPECTED_TARGET_REGION,
        "environment_identity": OBSERVED_ENVIRONMENT_IDENTITY,
        "issued_at": "2026-07-20T11:45:00Z",
        "expires_at": "2026-07-20T12:15:00Z",
        "nonce": "nonce-abc-123",
        "approved_by": "alice@example.com",
    }
    base.update(overrides)
    return base


def _verify(envelope: object, *, now: datetime = NOW, ledger=None):
    return verify_approval_envelope(
        envelope=envelope,
        now=now,
        expected_command=EXPECTED_COMMAND,
        expected_source_backup_id=EXPECTED_SOURCE_BACKUP_ID,
        expected_target_account=EXPECTED_TARGET_ACCOUNT,
        expected_target_region=EXPECTED_TARGET_REGION,
        observed_environment_identity=OBSERVED_ENVIRONMENT_IDENTITY,
        nonce_ledger=ledger if ledger is not None else InMemoryNonceLedger(),
    )


def test_absent_envelope_is_red() -> None:
    result = _verify(None)
    assert result.status == "RED"
    assert result.reasons == ("envelope_absent",)


def test_non_object_envelope_is_red() -> None:
    result = _verify(["not", "an", "object"])
    assert result.status == "RED"
    assert result.reasons == ("envelope_malformed:not_an_object",)


@pytest.mark.parametrize("field", _ENVELOPE_REQUIRED_FIELDS)
def test_missing_required_field_is_red(field: str) -> None:
    envelope = _valid_envelope()
    del envelope[field]
    result = _verify(envelope)
    assert result.status == "RED"
    assert result.reasons == (f"envelope_field_missing:{field}",)


def test_empty_string_field_is_red_same_as_missing() -> None:
    envelope = _valid_envelope(approved_by="")
    result = _verify(envelope)
    assert result.status == "RED"
    assert result.reasons == ("envelope_field_missing:approved_by",)


def test_unparseable_expires_at_is_red() -> None:
    envelope = _valid_envelope(expires_at="not-a-timestamp")
    result = _verify(envelope)
    assert result.status == "RED"
    assert result.reasons == ("envelope_expires_at_unparseable",)


def test_expired_envelope_is_red() -> None:
    envelope = _valid_envelope(expires_at="2026-07-20T11:00:00Z")  # before NOW
    result = _verify(envelope)
    assert result.status == "RED"
    assert result.reasons[0].startswith("envelope_expired")


def test_exactly_at_expiry_is_not_expired() -> None:
    envelope = _valid_envelope(expires_at=NOW.isoformat().replace("+00:00", "Z"))
    result = _verify(envelope)
    assert result.status == "GREEN"


def test_command_digest_mismatch_is_red() -> None:
    envelope = _valid_envelope(command_digest="0" * 64)
    result = _verify(envelope)
    assert result.status == "RED"
    assert result.reasons == ("command_digest_mismatch",)


def test_source_backup_id_mismatch_is_red() -> None:
    envelope = _valid_envelope(source_backup_id="backup-999")
    result = _verify(envelope)
    assert result.status == "RED"
    assert result.reasons == ("source_backup_id_mismatch",)


def test_target_account_mismatch_is_red() -> None:
    envelope = _valid_envelope(target_account="999999999999")
    result = _verify(envelope)
    assert result.status == "RED"
    assert result.reasons == ("target_account_mismatch",)


def test_target_region_mismatch_is_red() -> None:
    envelope = _valid_envelope(target_region="eu-west-1")
    result = _verify(envelope)
    assert result.status == "RED"
    assert result.reasons == ("target_region_mismatch",)


def test_environment_identity_mismatch_is_red() -> None:
    envelope = _valid_envelope(environment_identity="wrong-env")
    result = _verify(envelope)
    assert result.status == "RED"
    assert result.reasons == ("environment_identity_mismatch",)


def test_replayed_nonce_is_red() -> None:
    ledger = InMemoryNonceLedger()
    envelope = _valid_envelope()

    first = _verify(envelope, ledger=ledger)
    assert first.status == "GREEN"

    second = _verify(envelope, ledger=ledger)
    assert second.status == "RED"
    assert second.reasons == ("nonce_replayed",)


def test_rejected_envelope_does_not_consume_nonce() -> None:
    """A mismatched submission must not burn the nonce for a later, valid one."""
    ledger = InMemoryNonceLedger()
    bad_envelope = _valid_envelope(command_digest="0" * 64)
    rejected = _verify(bad_envelope, ledger=ledger)
    assert rejected.status == "RED"

    good_envelope = _valid_envelope()  # same nonce
    accepted = _verify(good_envelope, ledger=ledger)
    assert accepted.status == "GREEN"


def test_valid_envelope_is_green() -> None:
    result = _verify(_valid_envelope())
    assert result.status == "GREEN"
    assert result.reasons == ()


def test_now_injected_never_wall_clock() -> None:
    """now is an explicit parameter — the same envelope is expired or not
    purely as a function of the injected value, proving no hidden
    datetime.now() call is involved."""
    envelope = _valid_envelope()
    before_expiry = _verify(envelope, now=NOW)
    after_expiry = _verify(envelope, now=NOW + timedelta(hours=1))
    assert before_expiry.status == "GREEN"
    assert after_expiry.status == "RED"


# ---------------------------------------------------------------------------
# FileNonceLedger: fail-closed on corruption, atomic writes
# ---------------------------------------------------------------------------


class TestFileNonceLedger:
    def test_missing_file_is_legitimately_empty(self, tmp_path: Path) -> None:
        """No prior ledger file is a genuinely never-used ledger, not
        corruption — the first use of a nonce must be GREEN."""
        ledger = FileNonceLedger(tmp_path / "nonce-ledger.json")
        assert ledger.is_consumed("n1") is False

    def test_truncated_json_raises_corrupt(self, tmp_path: Path) -> None:
        path = tmp_path / "nonce-ledger.json"
        path.write_bytes(b'["n1", "n2"')
        ledger = FileNonceLedger(path)
        with pytest.raises(NonceLedgerCorruptError):
            ledger.is_consumed("n1")

    def test_zero_byte_file_raises_corrupt(self, tmp_path: Path) -> None:
        path = tmp_path / "nonce-ledger.json"
        path.write_bytes(b"")
        ledger = FileNonceLedger(path)
        with pytest.raises(NonceLedgerCorruptError):
            ledger.is_consumed("n1")

    def test_non_utf8_bytes_raises_corrupt(self, tmp_path: Path) -> None:
        path = tmp_path / "nonce-ledger.json"
        path.write_bytes(b"\xff\xfe\x00\x01")
        ledger = FileNonceLedger(path)
        with pytest.raises(NonceLedgerCorruptError):
            ledger.is_consumed("n1")

    def test_oversized_file_raises_corrupt(self, tmp_path: Path) -> None:
        path = tmp_path / "nonce-ledger.json"
        oversized_payload = json.dumps([f"nonce-{i}" for i in range(200_000)])
        assert len(oversized_payload.encode("utf-8")) > 1_000_000
        path.write_text(oversized_payload, encoding="utf-8")
        ledger = FileNonceLedger(path)
        with pytest.raises(NonceLedgerCorruptError):
            ledger.is_consumed("n1")

    def test_non_list_json_raises_corrupt(self, tmp_path: Path) -> None:
        path = tmp_path / "nonce-ledger.json"
        path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
        ledger = FileNonceLedger(path)
        with pytest.raises(NonceLedgerCorruptError):
            ledger.is_consumed("n1")

    def test_consume_writes_atomically_no_tmp_residue(self, tmp_path: Path) -> None:
        path = tmp_path / "nonce-ledger.json"
        ledger = FileNonceLedger(path)
        ledger.consume("n1")
        assert ledger.is_consumed("n1") is True
        assert not (tmp_path / "nonce-ledger.json.tmp").exists()

    def test_crash_during_replace_leaves_prior_valid_state_not_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crash between the temp-file write and the atomic rename must
        never silently reset the ledger to empty — the un-replaced original
        file (still holding the previously consumed nonce) is untouched."""
        path = tmp_path / "nonce-ledger.json"
        ledger = FileNonceLedger(path)
        ledger.consume("n1")

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise OSError("simulated crash during os.replace")

        monkeypatch.setattr(qualify_backup_restore.os, "replace", _boom)
        with pytest.raises(OSError):
            ledger.consume("n2")

        monkeypatch.undo()
        assert ledger.is_consumed("n1") is True
        assert ledger.is_consumed("n2") is False


def test_corrupt_nonce_ledger_causes_envelope_red_not_replay_bypass(tmp_path: Path) -> None:
    """The exploit scenario the fail-open bug enabled: consume a nonce,
    corrupt the durable ledger (crash/truncation), then resubmit the same
    already-used envelope. It must be RED nonce_ledger_corrupt, never a
    GREEN produced by a silently-reset-to-empty consumed set."""
    ledger_path = tmp_path / "nonce-ledger.json"
    ledger = FileNonceLedger(ledger_path)
    envelope = _valid_envelope()

    first = _verify(envelope, ledger=ledger)
    assert first.status == "GREEN"

    ledger_path.write_bytes(b"{not valid json")

    second = _verify(envelope, ledger=ledger)
    assert second.status == "RED"
    assert len(second.reasons) == 1
    assert second.reasons[0].startswith("nonce_ledger_corrupt:")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _write_json(tmp_path: Path, payload: object, name: str) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _cli_args(
    tmp_path: Path, envelope_path: Path, *, now: str = "2026-07-20T12:00:00Z"
) -> list[str]:
    return [
        "--envelope",
        str(envelope_path),
        "--command",
        EXPECTED_COMMAND,
        "--source-backup-id",
        EXPECTED_SOURCE_BACKUP_ID,
        "--target-account",
        EXPECTED_TARGET_ACCOUNT,
        "--target-region",
        EXPECTED_TARGET_REGION,
        "--environment-identity",
        OBSERVED_ENVIRONMENT_IDENTITY,
        "--now",
        now,
        "--nonce-ledger-path",
        str(tmp_path / "nonce-ledger.json"),
    ]


def test_cli_green_on_valid_envelope(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    envelope_path = _write_json(tmp_path, _valid_envelope(), "envelope.json")
    exit_code = cli_main(_cli_args(tmp_path, envelope_path))
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "GREEN" in captured.out


def test_cli_red_on_expired_envelope(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    envelope_path = _write_json(
        tmp_path, _valid_envelope(expires_at="2020-01-01T00:00:00Z"), "envelope.json"
    )
    exit_code = cli_main(_cli_args(tmp_path, envelope_path))
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "envelope_expired" in captured.err


def test_cli_replayed_nonce_across_two_invocations_is_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    envelope_path = _write_json(tmp_path, _valid_envelope(), "envelope.json")
    args = _cli_args(tmp_path, envelope_path)

    first_exit = cli_main(args)
    capsys.readouterr()
    assert first_exit == 0

    second_exit = cli_main(args)
    captured = capsys.readouterr()
    assert second_exit == 1
    assert "nonce_replayed" in captured.err


def test_cli_missing_required_arguments_is_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    envelope_path = _write_json(tmp_path, _valid_envelope(), "envelope.json")
    exit_code = cli_main(["--envelope", str(envelope_path)])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "cli_missing_required_envelope_arguments" in captured.err


def test_cli_empty_string_required_argument_is_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty string is not None — `is None` alone would let a blank
    required binding field (e.g. --command "") through to bind against a
    vacuous expected value."""
    envelope_path = _write_json(tmp_path, _valid_envelope(), "envelope.json")
    args = _cli_args(tmp_path, envelope_path)
    args[args.index("--command") + 1] = ""
    exit_code = cli_main(args)
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "cli_missing_required_envelope_arguments" in captured.err


def test_cli_whitespace_only_required_argument_is_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    envelope_path = _write_json(tmp_path, _valid_envelope(), "envelope.json")
    args = _cli_args(tmp_path, envelope_path)
    args[args.index("--target-account") + 1] = "   "
    exit_code = cli_main(args)
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "cli_missing_required_envelope_arguments" in captured.err


def test_cli_corrupt_nonce_ledger_is_red_not_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    envelope_path = _write_json(tmp_path, _valid_envelope(), "envelope.json")
    args = _cli_args(tmp_path, envelope_path)
    ledger_path = tmp_path / "nonce-ledger.json"
    ledger_path.write_bytes(b"not json at all")
    exit_code = cli_main(args)
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "nonce_ledger_corrupt" in captured.err
    assert "Traceback" not in captured.err
