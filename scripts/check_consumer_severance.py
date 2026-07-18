#!/usr/bin/env python3
"""Read-only producer-side verifier for MCP v1 consumer-severance receipts.

AC-SEV-5: "verification accepts only explicit revision and receipt inputs
and cannot edit, commit, push, or broaden scope in Omnius or SRE." This
script never opens, clones, or writes to a consumer repository — it takes
two pieces of data only: an exact 40-hex consumer git revision string, and
a path to a JSON "severance receipt" file the consumer produced elsewhere.
It never writes, creates, or deletes any file, and never shells out to git,
network, or any subprocess. Every code path that lacks a usable input
returns a RED decision-return (see `VerificationResult`) — it never raises
an uncaught exception and never fabricates a GREEN.

Severance receipt format (JSON object)
---------------------------------------
    {
      "consumer_revision":    "<40-hex immutable git commit>",
      "fixture_matrix_sha256": "<64-hex sha256 of the producer fixture matrix>",
      "command":               "<exact command the consumer ran>",
      "results": [
        {"id": "<fixture id>", "decision": "<consumer's observed decision>"},
        ...
      ]
    }

A receipt is GREEN only if:
  1. `consumer_revision` is a 40-hex string (no "-dirty"/uncommitted marker)
     and matches the `--consumer-revision` the caller supplied — immutable,
     pinned evidence, not a mutable working tree.
  2. `fixture_matrix_sha256` matches this producer's current fixture matrix
     digest (`tests/conformance/consumer_severance/fixtures.py`) —
     content-addressed, so a stale/tampered receipt cannot pass silently.
  3. `results` covers every fixture id in the matrix exactly (no partial
     coverage, no unexpected extra ids).
  4. Every result's `decision` matches this producer's expected decision
     for that fixture id.

Absent, dirty, mutable, or partial evidence is RED — a decision return, per
docs/specs/gh-issue-350-consumer-severance.md ("A missing consumer
revision, direct-source profile, owner approval, or safe drill target
produces a decision return and RED receipt. It does not authorize this
agent to edit another repository.").
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.conformance.consumer_severance.fixtures import (  # noqa: E402
    all_ids,
    expected_decisions,
    fixture_matrix_sha256,
)

_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")

# A well-formed receipt (fixture-matrix results plus a handful of scalar
# fields) is a few KB. 1,000,000 bytes is generous headroom over any real
# receipt while still bounding a truncated/corrupted or adversarial read —
# mirrors the MCP contract's own fixed 80,000-byte response budget
# (server.py:158) in spirit, scaled up since a receipt legitimately embeds
# the full fixture matrix.
_MAX_RECEIPT_BYTES = 1_000_000


@dataclass(frozen=True)
class VerificationResult:
    """A decision return — never an exception, always GREEN or RED."""

    status: str  # "GREEN" | "RED"
    reasons: tuple[str, ...] = ()

    @property
    def is_green(self) -> bool:
        return self.status == "GREEN"


def _load_receipt(receipt_path: Path) -> tuple[dict[str, object] | None, str | None]:
    try:
        size = receipt_path.stat().st_size
    except OSError as exc:
        return None, f"receipt_unreadable:{exc}"
    if size > _MAX_RECEIPT_BYTES:
        return None, f"receipt_too_large:{size}>{_MAX_RECEIPT_BYTES}"
    try:
        raw_bytes = receipt_path.read_bytes()
    except OSError as exc:
        return None, f"receipt_unreadable:{exc}"
    try:
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, f"receipt_not_utf8:{exc}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"receipt_malformed:{exc}"
    if not isinstance(parsed, dict):
        return None, "receipt_malformed:not_an_object"
    return parsed, None


def verify(*, consumer_revision: str | None, receipt_path: Path | None) -> VerificationResult:
    """Pure, read-only check. Never touches the filesystem except one read."""
    if not consumer_revision or not _REVISION_RE.fullmatch(consumer_revision):
        return VerificationResult("RED", (f"revision_not_immutable:{consumer_revision!r}",))

    if receipt_path is None:
        return VerificationResult("RED", ("receipt_absent",))
    if not receipt_path.is_file():
        return VerificationResult("RED", (f"receipt_absent:{receipt_path}",))

    receipt, load_error = _load_receipt(receipt_path)
    if receipt is None:
        return VerificationResult("RED", (load_error or "receipt_malformed:unknown",))

    receipt_revision = receipt.get("consumer_revision")
    if receipt_revision != consumer_revision:
        return VerificationResult(
            "RED",
            (f"revision_mismatch:expected={consumer_revision}:receipt={receipt_revision!r}",),
        )
    if not isinstance(receipt_revision, str) or not _REVISION_RE.fullmatch(receipt_revision):
        return VerificationResult("RED", (f"revision_not_immutable:receipt={receipt_revision!r}",))

    command = receipt.get("command")
    if not isinstance(command, str) or not command.strip():
        return VerificationResult("RED", ("receipt_command_missing",))

    expected_digest = fixture_matrix_sha256()
    receipt_digest = receipt.get("fixture_matrix_sha256")
    if receipt_digest != expected_digest:
        return VerificationResult(
            "RED",
            (f"fixture_digest_mismatch:expected={expected_digest}:receipt={receipt_digest!r}",),
        )

    results = receipt.get("results")
    if not isinstance(results, list):
        return VerificationResult("RED", ("receipt_malformed:results_not_list",))

    observed: dict[str, object] = {}
    for row in results:
        if not isinstance(row, dict) or "id" not in row or "decision" not in row:
            return VerificationResult("RED", ("receipt_malformed:result_row_shape",))
        row_id = row["id"]
        if not isinstance(row_id, str) or not row_id:
            return VerificationResult("RED", ("receipt_malformed:result_row_shape",))
        observed[row_id] = row["decision"]

    expected_ids = all_ids()
    observed_ids = set(observed)
    missing = expected_ids - observed_ids
    unexpected = observed_ids - expected_ids
    if missing or unexpected:
        reasons: list[str] = []
        if missing:
            reasons.append(f"receipt_partial:missing={sorted(missing)}")
        if unexpected:
            reasons.append(f"receipt_partial:unexpected={sorted(unexpected)}")
        return VerificationResult("RED", tuple(reasons))

    expected = expected_decisions()
    mismatches = tuple(
        f"decision_mismatch:{fixture_id}:expected={expected[fixture_id]}:"
        f"observed={observed[fixture_id]!r}"
        for fixture_id in sorted(expected_ids)
        if observed[fixture_id] != expected[fixture_id]
    )
    if mismatches:
        return VerificationResult("RED", mismatches)

    return VerificationResult("GREEN")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--consumer-revision",
        default=None,
        help="Exact 40-hex immutable consumer git revision the receipt must pin against.",
    )
    parser.add_argument(
        "--receipt",
        type=Path,
        default=None,
        help="Path to the consumer's content-addressed severance receipt (JSON).",
    )
    args = parser.parse_args(argv)

    result = verify(consumer_revision=args.consumer_revision, receipt_path=args.receipt)
    if result.is_green:
        print("consumer-severance verification: GREEN")
        return 0

    print("consumer-severance verification: RED (decision-return)", file=sys.stderr)
    for reason in result.reasons:
        print(f"- {reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
