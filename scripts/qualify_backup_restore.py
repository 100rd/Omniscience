#!/usr/bin/env python3
"""Backup/restore qualification harness (AC-DR-1, AC-DR-2, AC-DR-3, AC-DR-5).

This module is a free-standing, framework-free qualification harness for
PostgreSQL-authoritative backup/restore. It NEVER performs a destructive
operation, NEVER opens a cloud SDK connection, and NEVER stores or requires
cloud credentials. Every public check is a pure function that takes explicit
inputs (JSON evidence files, injected fixtures/fakes) and returns a frozen
``VerificationResult`` — it never raises an uncaught exception and never
fabricates a GREEN. An absent input is a typed RED result, not a placeholder
(docs/specs/gh-issue-350-backup-restore.md, "Execution order and required
inputs").

Public surface
--------------
VerificationResult            — frozen GREEN/RED decision-return, never raised
check_backup_policy_evidence  — AC-DR-1: backup policy names every SPEC-SOT
                                 authority table + immutable-copy metadata
verify_approval_envelope      — AC-DR-2: one-shot destructive-approval envelope
check_destructive_safety      — AC-DR-3: production-name/disposable/non-empty/
                                 writer-lease/credential-ambiguity refusal
DrillManifest                 — AC-DR-5: frozen, content-addressed drill record
build_manifest                — construct a DrillManifest from measured inputs
validate_manifest_json        — re-validate a serialized manifest artifact
NonceLedger / WriterLeaseSource / CredentialResolver — injectable Protocols
AmbiguousIdentityError         — raised by a CredentialResolver, never escapes
                                  check_destructive_safety uncaught
NonceLedgerCorruptError         — raised by a NonceLedger on corrupted durable
                                  state; verify_approval_envelope catches it
                                  and returns RED, never an empty consumed-set

What this module deliberately does NOT do
------------------------------------------
It does not perform a live destructive restore, does not bind to a real
cloud identity provider, and does not execute a full measured RPO/RTO drill.
Those require an isolated recovery environment and a separate, fresh,
one-shot human approval this agent cannot self-grant — see
docs/runbooks/backup-restore.md "Blocked on external — decision returns".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dr_verify import DRVerificationResult, RtoResult  # noqa: E402

# A qualification evidence file (policy, envelope, or manifest) is a small
# JSON object. 1,000,000 bytes is generous headroom over any real evidence
# file while still bounding a truncated/corrupted or adversarial read —
# mirrors scripts/check_consumer_severance.py's own budget.
_MAX_EVIDENCE_BYTES = 1_000_000

#: Authority tables required by specs/SPEC-SOT-ledger-and-projections.md
#: [REQ-SOT-1] ("Postgres owns source/document/chunk content and lineage,
#: versions, entity/outbox records, workspaces, and governance metadata
#: required to rebuild projections") and [REQ-SOT-8] ("Backup/restore
#: preserves every field needed for projection rebuild and tenant/audit
#: history"). This is NOT "every __tablename__ in db/models.py" and NOT
#: "whatever rebuild_all_projections.py happens to import" — both of those
#: would silently under- or over-claim what REQ-SOT-1/8 actually names.
#: The ORM defines eleven tables total (ten in db/models.py, one in
#: audit/models.py); this constant includes ten of them, each mapped to a
#: REQ-SOT-1/8 clause:
#:   - workspaces, sources, documents, chunks: "source/document/chunk
#:     content", "workspaces"
#:   - ingestion_runs: "lineage" (per-source ingestion history that chunks
#:     reference via ingestion_run_id)
#:   - entities, edges: "entity/outbox records" (the entity/edge half)
#:   - outbox_events: "entity/outbox records" (the outbox half)
#:   - entity_emitter: "governance metadata required to rebuild
#:     projections" (the authority_emitter state machine a projection
#:     consumer uses to accept/drop an incoming entity write)
#:   - audit_log (packages/core/src/omniscience_core/audit/models.py):
#:     "tenant and audit history" verbatim — an append-only,
#:     workspace_id-scoped record of every audited tool invocation
#: Excluded: api_tokens. It is authentication/authorization credential
#: material (hashed tokens, scopes) — not ledger content, lineage, an
#: entity/outbox record, or governance metadata that a projection rebuild
#: or an audit-history claim depends on. If the schema grows a new
#: authority table, this constant (and only this constant) must move.
AUTHORITY_TABLES: frozenset[str] = frozenset(
    {
        "workspaces",
        "sources",
        "documents",
        "ingestion_runs",
        "chunks",
        "entities",
        "edges",
        "entity_emitter",
        "outbox_events",
        "audit_log",
    }
)

# Case-insensitive substring match on "prod" or "live" — deliberately not
# \b-word-bounded. A \b boundary treats digits as word characters, so
# \bprod\b misses prod1/prod01/production2/prodenv/liveenv, all ordinary
# cloud-resource naming patterns (numeric suffixes, concatenated env+role
# names). AC-DR-3 requires unconditional refusal on a production-like name;
# a substring match over-flags a name like "olive-branch" but that is the
# correct bias here — a false-positive refusal costs a renamed disposable
# target, a false-negative lets a wipe hit production.
_PRODUCTION_LIKE_PATTERN = re.compile(r"(?i)(prod|live)")


@dataclass(frozen=True)
class VerificationResult:
    """A decision return — never an exception, always GREEN or RED."""

    status: str  # "GREEN" | "RED"
    reasons: tuple[str, ...] = ()

    @property
    def is_green(self) -> bool:
        return self.status == "GREEN"

    @property
    def abort(self) -> bool:
        """True when the destructive path must abort (RED)."""
        return not self.is_green


def _load_json_evidence(path: Path | None) -> tuple[Any | None, str | None]:
    """Load and parse a JSON evidence file with the same guards as
    scripts/check_consumer_severance.py::_load_receipt (byte cap, strict
    UTF-8, strict JSON). Returns (parsed, None) or (None, reason)."""
    if path is None:
        return None, "absent"
    try:
        size = path.stat().st_size
    except OSError as exc:
        return None, f"unreadable:{exc}"
    if size > _MAX_EVIDENCE_BYTES:
        return None, f"too_large:{size}>{_MAX_EVIDENCE_BYTES}"
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        return None, f"unreadable:{exc}"
    try:
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, f"not_utf8:{exc}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"malformed:{exc}"
    return parsed, None


# ---------------------------------------------------------------------------
# AC-DR-1: backup-policy evidence
# ---------------------------------------------------------------------------


def check_backup_policy_evidence(policy: Any | None) -> VerificationResult:
    """Verify a backup-policy/manifest evidence object names every SPEC-SOT
    authority table and carries immutable-copy ownership, retention,
    encryption, cross-account/region copies, and a measured recovery point.

    ``policy`` is the already-parsed JSON object (or None if absent/unreadable
    — callers should use ``_load_json_evidence`` first). Missing/empty/
    wrong-typed fields are RED with a specific reason each; nothing is
    silently assumed complete.
    """
    if policy is None:
        return VerificationResult("RED", ("policy_absent",))
    if not isinstance(policy, dict):
        return VerificationResult("RED", ("policy_malformed:not_an_object",))

    reasons: list[str] = []

    raw_tables = policy.get("authority_tables")
    if not isinstance(raw_tables, list) or not all(isinstance(t, str) for t in raw_tables):
        reasons.append("authority_tables_absent_or_malformed")
        named_tables: frozenset[str] = frozenset()
    else:
        named_tables = frozenset(raw_tables)
    missing_tables = AUTHORITY_TABLES - named_tables
    for table in sorted(missing_tables):
        reasons.append(f"authority_table_missing:{table}")

    immutable_copy = policy.get("immutable_copy")
    if not isinstance(immutable_copy, dict):
        reasons.append("immutable_copy_absent_or_malformed")
        immutable_copy = {}
    owner = immutable_copy.get("owner")
    if not isinstance(owner, str) or not owner.strip():
        reasons.append("immutable_copy_owner_absent")
    retention_days = immutable_copy.get("retention_days")
    if (
        not isinstance(retention_days, int)
        or isinstance(retention_days, bool)
        or retention_days <= 0
    ):
        reasons.append("immutable_copy_retention_days_absent_or_invalid")
    encryption = immutable_copy.get("encryption")
    if not isinstance(encryption, str) or not encryption.strip():
        reasons.append("immutable_copy_encryption_absent")

    cross_account = policy.get("cross_account_copy")
    account = cross_account.get("account") if isinstance(cross_account, dict) else None
    if not isinstance(account, str) or not account.strip():
        reasons.append("cross_account_copy_absent")

    cross_region = policy.get("cross_region_copy")
    region = cross_region.get("region") if isinstance(cross_region, dict) else None
    if not isinstance(region, str) or not region.strip():
        reasons.append("cross_region_copy_absent")

    recovery_point = policy.get("measured_recovery_point_seconds")
    if (
        not isinstance(recovery_point, (int, float))
        or isinstance(recovery_point, bool)
        or recovery_point < 0
    ):
        reasons.append("measured_recovery_point_absent_or_invalid")

    if reasons:
        return VerificationResult("RED", tuple(reasons))
    return VerificationResult("GREEN")


# ---------------------------------------------------------------------------
# AC-DR-2: destructive-command approval envelope
# ---------------------------------------------------------------------------

_ENVELOPE_REQUIRED_FIELDS: tuple[str, ...] = (
    "command_digest",
    "source_backup_id",
    "target_account",
    "target_region",
    "environment_identity",
    "issued_at",
    "expires_at",
    "nonce",
    "approved_by",
)


class NonceLedger(Protocol):
    """Append-only redemption ledger — a nonce may be consumed exactly once."""

    def is_consumed(self, nonce: str) -> bool: ...

    def consume(self, nonce: str) -> None: ...


class InMemoryNonceLedger:
    """Process-local NonceLedger. Real drills need a durable ledger; this is
    sufficient for a single-process CLI invocation or a unit test fake."""

    def __init__(self) -> None:
        self._consumed: set[str] = set()

    def is_consumed(self, nonce: str) -> bool:
        return nonce in self._consumed

    def consume(self, nonce: str) -> None:
        self._consumed.add(nonce)


class NonceLedgerCorruptError(RuntimeError):
    """Raised by a NonceLedger when its durable state cannot be trusted
    (unreadable, oversized, non-UTF-8, malformed JSON, or wrong-shaped).

    A NonceLedger MUST fail closed on corruption, never fall back to an
    empty consumed-set: an empty set means "nothing has ever been
    consumed," which would let a previously-redeemed nonce (and the
    destructive approval it was attached to) pass as fresh again.
    """


class FileNonceLedger:
    """Durable NonceLedger backed by a JSON array file of consumed nonces.

    Suitable for the CLI where successive one-shot invocations must share
    redemption state without a database. Not suitable for concurrent writers
    (no locking) — a real drill runner should back this with the same
    durable store as the rest of its redemption audit trail.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def _read(self) -> set[str]:
        """Read the consumed-nonce set. A missing file is a legitimate,
        never-yet-used ledger (empty set). Any other unreadable/oversized/
        malformed state raises NonceLedgerCorruptError — it is never
        silently treated as "nothing consumed."
        """
        if not self._path.is_file():
            return set()
        try:
            size = self._path.stat().st_size
        except OSError as exc:
            raise NonceLedgerCorruptError(f"unreadable:{exc}") from exc
        if size > _MAX_EVIDENCE_BYTES:
            raise NonceLedgerCorruptError(f"too_large:{size}>{_MAX_EVIDENCE_BYTES}")
        try:
            raw_bytes = self._path.read_bytes()
        except OSError as exc:
            raise NonceLedgerCorruptError(f"unreadable:{exc}") from exc
        try:
            raw = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise NonceLedgerCorruptError(f"not_utf8:{exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise NonceLedgerCorruptError(f"malformed:{exc}") from exc
        if not isinstance(data, list) or not all(isinstance(n, str) for n in data):
            raise NonceLedgerCorruptError("not_a_list_of_strings")
        return set(data)

    def is_consumed(self, nonce: str) -> bool:
        return nonce in self._read()

    def consume(self, nonce: str) -> None:
        consumed = self._read()
        consumed.add(nonce)
        # Atomic write: a crash/SIGKILL/disk-full between these two steps
        # must never leave self._path truncated or partially written — the
        # rename is the only step that can touch the real path, and it is
        # atomic on the same filesystem.
        tmp_path = self._path.parent / f"{self._path.name}.tmp"
        tmp_path.write_text(json.dumps(sorted(consumed)), encoding="utf-8")
        os.replace(tmp_path, self._path)


def compute_command_digest(command: str) -> str:
    """Return the sha256 hex digest of the exact destructive command string."""
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def _parse_rfc3339(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def verify_approval_envelope(
    *,
    envelope: Any | None,
    now: datetime,
    expected_command: str,
    expected_source_backup_id: str,
    expected_target_account: str,
    expected_target_region: str,
    observed_environment_identity: str,
    nonce_ledger: NonceLedger,
) -> VerificationResult:
    """Verify a one-shot destructive-command approval envelope.

    ``now`` is injected (never ``datetime.now()`` internally) so the check is
    deterministic and testable. ``observed_environment_identity`` must come
    from a source independent of the envelope itself (a live identity check
    in a real drill; a fixture value in a test) — the envelope's own claim is
    never trusted as its own proof.

    Absent, expired, mismatched, or replayed approval returns RED with a
    field-specific reason and never mutates the nonce ledger. Only a fully
    valid envelope consumes its nonce (on the same call that returns GREEN),
    so a rejected submission never burns a legitimate nonce.
    """
    if envelope is None:
        return VerificationResult("RED", ("envelope_absent",))
    if not isinstance(envelope, dict):
        return VerificationResult("RED", ("envelope_malformed:not_an_object",))

    missing = [
        f
        for f in _ENVELOPE_REQUIRED_FIELDS
        if not isinstance(envelope.get(f), str) or not envelope.get(f)
    ]
    if missing:
        return VerificationResult("RED", tuple(f"envelope_field_missing:{f}" for f in missing))

    expires_at = _parse_rfc3339(envelope["expires_at"])
    if expires_at is None:
        return VerificationResult("RED", ("envelope_expires_at_unparseable",))
    if now > expires_at:
        return VerificationResult(
            "RED", (f"envelope_expired:expires_at={envelope['expires_at']}",)
        )

    expected_digest = compute_command_digest(expected_command)
    if envelope["command_digest"] != expected_digest:
        return VerificationResult("RED", ("command_digest_mismatch",))
    if envelope["source_backup_id"] != expected_source_backup_id:
        return VerificationResult("RED", ("source_backup_id_mismatch",))
    if envelope["target_account"] != expected_target_account:
        return VerificationResult("RED", ("target_account_mismatch",))
    if envelope["target_region"] != expected_target_region:
        return VerificationResult("RED", ("target_region_mismatch",))
    if envelope["environment_identity"] != observed_environment_identity:
        return VerificationResult("RED", ("environment_identity_mismatch",))

    nonce = envelope["nonce"]
    try:
        already_consumed = nonce_ledger.is_consumed(nonce)
    except NonceLedgerCorruptError as exc:
        return VerificationResult("RED", (f"nonce_ledger_corrupt:{exc}",))
    if already_consumed:
        return VerificationResult("RED", ("nonce_replayed",))

    try:
        nonce_ledger.consume(nonce)
    except NonceLedgerCorruptError as exc:
        return VerificationResult("RED", (f"nonce_ledger_corrupt:{exc}",))
    return VerificationResult("GREEN")


# ---------------------------------------------------------------------------
# AC-DR-3: destructive-safety refusal
# ---------------------------------------------------------------------------


class AmbiguousIdentityError(Exception):
    """Raised by a CredentialResolver when more than one candidate cloud
    identity resolves for the operation, or the identity cannot be uniquely
    resolved."""


class WriterLeaseSource(Protocol):
    """Reports whether a live writer/consumer lease is currently held."""

    def has_active_writers(self) -> bool: ...


class CredentialResolver(Protocol):
    """Resolves the single cloud identity the destructive command would run
    as. Must raise AmbiguousIdentityError when more than one candidate
    resolves."""

    def resolve_identity(self) -> str: ...


def check_destructive_safety(
    *,
    target_name: str | None = None,
    target_tags: dict[str, str] | None = None,
    qdrant_chunk_count: int,
    neo4j_entity_count: int,
    drill_context: bool = False,
    writer_lease_source: WriterLeaseSource | None = None,
    credential_resolver: CredentialResolver | None = None,
) -> VerificationResult:
    """Refuse a destructive wipe/restore unless every available safety
    check passes. Each check is evaluated only when its input is supplied
    (``None`` means "not applicable in this caller's context", not "assume
    safe") — see module docstring for why rebuild_all_projections.py only
    exercises a subset of these checks today.

    ``drill_context=True`` authorizes wiping projections that are already
    non-empty (the ordinary DR-rebuild case: Neo4j/Qdrant hold stale data
    that is about to be replaced from the Postgres source of truth). It does
    NOT bypass the production-name, disposable-tag, writer-lease, or
    credential-ambiguity checks.
    """
    reasons: list[str] = []

    if target_name is not None and _PRODUCTION_LIKE_PATTERN.search(target_name):
        reasons.append(f"production_like_name:{target_name!r}")

    if target_tags is not None and target_tags.get("disposable") != "true":
        reasons.append("absent_disposable_identity")

    if not drill_context and (qdrant_chunk_count != 0 or neo4j_entity_count != 0):
        reasons.append(
            f"non_empty_projections:qdrant={qdrant_chunk_count}:neo4j={neo4j_entity_count}"
        )

    if writer_lease_source is not None:
        try:
            if writer_lease_source.has_active_writers():
                reasons.append("active_writers_detected")
        except Exception as exc:
            reasons.append(f"writer_lease_check_failed:{exc}")

    if credential_resolver is not None:
        try:
            credential_resolver.resolve_identity()
        except AmbiguousIdentityError as exc:
            reasons.append(f"ambiguous_credentials:{exc}")
        except Exception as exc:
            reasons.append(f"credential_resolution_failed:{exc}")

    if reasons:
        return VerificationResult("RED", tuple(reasons))
    return VerificationResult("GREEN")


# ---------------------------------------------------------------------------
# AC-DR-5: drill manifest
# ---------------------------------------------------------------------------


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


@dataclass(frozen=True)
class DrillManifest:
    """Content-addressed, tamper-evident record of a backup/restore drill.

    ``manifest_id`` is a self-addressing sha256 of every other field, so a
    tampered or partially-filled manifest cannot silently claim a different
    identity. Any field that could not be measured is ``None`` (an honest
    absence marker), never a fabricated placeholder — see ``build_manifest``.
    """

    manifest_id: str
    drill_started_at: str | None
    drill_completed_at: str | None
    measured_rpo_seconds: float | None
    measured_rto_seconds: float | None
    rto_budget_seconds: int | None
    rto_exceeded: bool | None
    source_backup_id: str | None
    restored_environment_identity: str | None
    verification_passed: bool | None
    verification_mismatches: tuple[str, ...]
    hash_equivalence_mismatches: tuple[str, ...]
    query_probes_passed: bool | None
    status: str  # "GREEN" | "RED"
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_id": self.manifest_id,
            "drill_started_at": self.drill_started_at,
            "drill_completed_at": self.drill_completed_at,
            "measured_rpo_seconds": self.measured_rpo_seconds,
            "measured_rto_seconds": self.measured_rto_seconds,
            "rto_budget_seconds": self.rto_budget_seconds,
            "rto_exceeded": self.rto_exceeded,
            "source_backup_id": self.source_backup_id,
            "restored_environment_identity": self.restored_environment_identity,
            "verification_passed": self.verification_passed,
            "verification_mismatches": list(self.verification_mismatches),
            "hash_equivalence_mismatches": list(self.hash_equivalence_mismatches),
            "query_probes_passed": self.query_probes_passed,
            "status": self.status,
            "reasons": list(self.reasons),
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())


def build_manifest(
    *,
    drill_started_at: str | None,
    drill_completed_at: str | None,
    measured_rpo_seconds: float | None,
    rto_result: RtoResult | None,
    source_backup_id: str | None,
    restored_environment_identity: str | None,
    verification_result: DRVerificationResult | None,
    hash_equivalence_mismatches: list[str] | None,
    query_probes_passed: bool | None,
) -> DrillManifest:
    """Build a DrillManifest from measured drill inputs.

    Every parameter is the caller's own measured/observed value — this
    function never invents one. A ``None``/absent input, an RTO breach, a
    failed verification, a non-empty hash-equivalence mismatch list, or
    ``query_probes_passed=False`` all produce ``status="RED"`` with a
    specific reason; nothing here silently defaults to a passing state.
    """
    reasons: list[str] = []

    if not drill_started_at:
        reasons.append("drill_started_at_absent")
    if not drill_completed_at:
        reasons.append("drill_completed_at_absent")
    if measured_rpo_seconds is None or measured_rpo_seconds < 0:
        reasons.append("measured_rpo_seconds_absent_or_invalid")

    if rto_result is None:
        reasons.append("rto_result_absent")
    elif rto_result.exceeded:
        reasons.append(
            f"rto_exceeded:elapsed={rto_result.elapsed_seconds:.1f}:"
            f"budget={rto_result.budget_seconds}"
        )

    if not source_backup_id:
        reasons.append("source_backup_id_absent")
    if not restored_environment_identity:
        reasons.append("restored_environment_identity_absent")

    if verification_result is None:
        reasons.append("verification_result_absent")
    elif not verification_result.passed:
        reasons.append("verification_failed")

    if hash_equivalence_mismatches is None:
        reasons.append("hash_equivalence_result_absent")
    elif hash_equivalence_mismatches:
        reasons.append("hash_equivalence_mismatch")

    if query_probes_passed is None:
        reasons.append("query_probes_absent")
    elif not query_probes_passed:
        reasons.append("query_probes_failed")

    status = "RED" if reasons else "GREEN"

    body: dict[str, Any] = {
        "drill_started_at": drill_started_at,
        "drill_completed_at": drill_completed_at,
        "measured_rpo_seconds": measured_rpo_seconds,
        "measured_rto_seconds": rto_result.elapsed_seconds if rto_result is not None else None,
        "rto_budget_seconds": rto_result.budget_seconds if rto_result is not None else None,
        "rto_exceeded": rto_result.exceeded if rto_result is not None else None,
        "source_backup_id": source_backup_id,
        "restored_environment_identity": restored_environment_identity,
        "verification_passed": (
            verification_result.passed if verification_result is not None else None
        ),
        "verification_mismatches": (
            list(verification_result.mismatches) if verification_result else []
        ),
        "hash_equivalence_mismatches": list(hash_equivalence_mismatches or []),
        "query_probes_passed": query_probes_passed,
        "status": status,
        "reasons": reasons,
    }
    content_hash = hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()

    return DrillManifest(
        manifest_id=f"sha256:{content_hash}",
        drill_started_at=body["drill_started_at"],
        drill_completed_at=body["drill_completed_at"],
        measured_rpo_seconds=body["measured_rpo_seconds"],
        measured_rto_seconds=body["measured_rto_seconds"],
        rto_budget_seconds=body["rto_budget_seconds"],
        rto_exceeded=body["rto_exceeded"],
        source_backup_id=body["source_backup_id"],
        restored_environment_identity=body["restored_environment_identity"],
        verification_passed=body["verification_passed"],
        verification_mismatches=tuple(body["verification_mismatches"]),
        hash_equivalence_mismatches=tuple(body["hash_equivalence_mismatches"]),
        query_probes_passed=body["query_probes_passed"],
        status=status,
        reasons=tuple(reasons),
    )


_MANIFEST_REQUIRED_FIELDS: tuple[str, ...] = (
    "manifest_id",
    "drill_started_at",
    "drill_completed_at",
    "measured_rpo_seconds",
    "measured_rto_seconds",
    "rto_budget_seconds",
    "rto_exceeded",
    "source_backup_id",
    "restored_environment_identity",
    "verification_passed",
    "query_probes_passed",
    "status",
)


def validate_manifest_json(manifest: Any | None) -> VerificationResult:
    """Re-validate an already-serialized drill manifest artifact (e.g. one
    produced by a prior drill run and committed as CI evidence). Distinct
    from ``build_manifest``: this only checks the artifact's own shape and
    declared status, never reconstructs one.
    """
    if manifest is None:
        return VerificationResult("RED", ("manifest_absent",))
    if not isinstance(manifest, dict):
        return VerificationResult("RED", ("manifest_malformed:not_an_object",))

    reasons: list[str] = []
    for f in _MANIFEST_REQUIRED_FIELDS:
        if f not in manifest or manifest[f] is None:
            reasons.append(f"manifest_field_absent:{f}")

    if manifest.get("rto_exceeded") is True:
        reasons.append("manifest_rto_exceeded")
    if manifest.get("verification_passed") is False:
        reasons.append("manifest_verification_failed")
    if manifest.get("query_probes_passed") is False:
        reasons.append("manifest_query_probes_failed")
    declared_status = manifest.get("status")
    if declared_status != "GREEN":
        reasons.append(f"manifest_status_not_green:{declared_status!r}")

    if reasons:
        return VerificationResult("RED", tuple(dict.fromkeys(reasons)))
    return VerificationResult("GREEN")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class _CliWriterLeaseSource:
    def __init__(self, active: bool) -> None:
        self._active = active

    def has_active_writers(self) -> bool:
        return self._active


class _CliCredentialResolver:
    def __init__(self, *, ambiguous: bool, identity: str) -> None:
        self._ambiguous = ambiguous
        self._identity = identity

    def resolve_identity(self) -> str:
        if self._ambiguous:
            raise AmbiguousIdentityError("multiple candidate identities resolved")
        return self._identity


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--policy", type=Path, default=None, help="AC-DR-1 backup-policy evidence JSON."
    )
    parser.add_argument(
        "--envelope", type=Path, default=None, help="AC-DR-2 approval envelope JSON."
    )
    parser.add_argument(
        "--command", default=None, help="Exact destructive command the envelope must pin."
    )
    parser.add_argument("--source-backup-id", default=None)
    parser.add_argument("--target-account", default=None)
    parser.add_argument("--target-region", default=None)
    parser.add_argument(
        "--environment-identity", default=None, help="Independently observed target identity."
    )
    parser.add_argument(
        "--now", default=None, help="RFC3339 timestamp used as 'now' for expiry checks."
    )
    parser.add_argument("--nonce-ledger-path", type=Path, default=None)
    parser.add_argument(
        "--target-name", default=None, help="AC-DR-3 refusal check: target identifier."
    )
    parser.add_argument(
        "--target-tags-json", default=None, help="AC-DR-3 refusal check: JSON object of tags."
    )
    parser.add_argument("--qdrant-chunk-count", type=int, default=None)
    parser.add_argument("--neo4j-entity-count", type=int, default=None)
    parser.add_argument("--drill-context", action="store_true")
    parser.add_argument("--active-writers", action="store_true")
    parser.add_argument("--ambiguous-credentials", action="store_true")
    parser.add_argument("--credential-identity", default="unknown")
    parser.add_argument(
        "--manifest", type=Path, default=None, help="AC-DR-5 manifest JSON to validate."
    )
    return parser


def _is_blank_required_arg(value: object) -> bool:
    """True for None or an empty/whitespace-only str/Path — a required
    binding field (command, target account/region, ...) must be genuinely
    present, not an empty string that would bind against a vacuous
    expected value (e.g. compute_command_digest(""))."""
    if value is None:
        return True
    if isinstance(value, (str, Path)):
        return not str(value).strip()
    return False


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    ran_any = False
    all_reasons: list[str] = []
    overall_green = True

    def _record(label: str, result: VerificationResult) -> None:
        nonlocal overall_green
        if not result.is_green:
            overall_green = False
            for reason in result.reasons:
                all_reasons.append(f"{label}:{reason}")

    if args.policy is not None:
        ran_any = True
        policy, load_error = _load_json_evidence(args.policy)
        if load_error is not None:
            _record("policy", VerificationResult("RED", (f"policy_{load_error}",)))
        else:
            _record("policy", check_backup_policy_evidence(policy))

    if args.envelope is not None:
        ran_any = True
        required = (
            args.command,
            args.source_backup_id,
            args.target_account,
            args.target_region,
            args.environment_identity,
            args.now,
            args.nonce_ledger_path,
        )
        if any(_is_blank_required_arg(v) for v in required):
            _record(
                "envelope",
                VerificationResult("RED", ("cli_missing_required_envelope_arguments",)),
            )
        else:
            envelope, load_error = _load_json_evidence(args.envelope)
            if load_error is not None:
                _record("envelope", VerificationResult("RED", (f"envelope_{load_error}",)))
            else:
                now = _parse_rfc3339(args.now)
                if now is None:
                    _record("envelope", VerificationResult("RED", ("cli_now_unparseable",)))
                else:
                    ledger = FileNonceLedger(args.nonce_ledger_path)
                    _record(
                        "envelope",
                        verify_approval_envelope(
                            envelope=envelope,
                            now=now,
                            expected_command=args.command,
                            expected_source_backup_id=args.source_backup_id,
                            expected_target_account=args.target_account,
                            expected_target_region=args.target_region,
                            observed_environment_identity=args.environment_identity,
                            nonce_ledger=ledger,
                        ),
                    )

    if (
        args.target_name is not None
        or args.qdrant_chunk_count is not None
        or args.neo4j_entity_count is not None
    ):
        ran_any = True
        if args.qdrant_chunk_count is None or args.neo4j_entity_count is None:
            _record("refusal", VerificationResult("RED", ("refusal_counts_required",)))
        else:
            target_tags: dict[str, str] | None = None
            if args.target_tags_json is not None:
                try:
                    parsed_tags = json.loads(args.target_tags_json)
                except json.JSONDecodeError:
                    _record("refusal", VerificationResult("RED", ("target_tags_malformed",)))
                    parsed_tags = None
                if isinstance(parsed_tags, dict):
                    target_tags = {str(k): str(v) for k, v in parsed_tags.items()}
            _record(
                "refusal",
                check_destructive_safety(
                    target_name=args.target_name,
                    target_tags=target_tags,
                    qdrant_chunk_count=args.qdrant_chunk_count,
                    neo4j_entity_count=args.neo4j_entity_count,
                    drill_context=args.drill_context,
                    writer_lease_source=_CliWriterLeaseSource(args.active_writers),
                    credential_resolver=_CliCredentialResolver(
                        ambiguous=args.ambiguous_credentials, identity=args.credential_identity
                    ),
                ),
            )

    if args.manifest is not None:
        ran_any = True
        manifest, load_error = _load_json_evidence(args.manifest)
        if load_error is not None:
            _record("manifest", VerificationResult("RED", (f"manifest_{load_error}",)))
        else:
            _record("manifest", validate_manifest_json(manifest))

    if not ran_any:
        print(
            "qualify-backup-restore: RED (decision-return) — no check requested", file=sys.stderr
        )
        print("- no_check_requested", file=sys.stderr)
        return 1

    if overall_green:
        print("qualify-backup-restore verification: GREEN")
        return 0

    print("qualify-backup-restore verification: RED (decision-return)", file=sys.stderr)
    for reason in all_reasons:
        print(f"- {reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
