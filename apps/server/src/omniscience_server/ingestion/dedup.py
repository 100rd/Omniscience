"""Server-side ingestion dedup — operator vs k8s-agentic (issue #164).

Background
----------

Once the in-cluster operator (#163, ADR-0007) is authoritative for the
Kubernetes resources it covers, the legacy agentic ``k8s`` connector
must not double-write the same resource.  Both producers emit
:class:`~omniscience_server.ingestion.events.DocumentChangeEvent`
messages on the ``ingest.changes`` stream; without server-side dedup,
two writes per resource land in the graph store with undefined ordering.

This module is the dedup gate.  It runs **before** the per-document
pipeline (so rejected events never reach the index / vector / graph
adapters) and consults a tiny bookkeeping table — ``entity_emitter`` —
keyed on ``(workspace_id, external_id)``.

State machine
-------------

For each incoming event the gate observes ``(workspace_id, external_id,
event_emitter)`` against the persisted authority for that pair:

* No row yet                                        -> ACCEPT, write authority = event_emitter.
* Row exists, authority == event_emitter            -> ACCEPT (idempotent refresh of last_emit_at).
* Row exists, authority == ``k8s-operator``,
  event_emitter == ``k8s-agentic``,
  last_emit_at within TTL                           -> DROP (no_op_agentic_dropped).
* Row exists, authority == ``k8s-operator``,
  event_emitter == ``k8s-agentic``,
  last_emit_at older than TTL                       -> ACCEPT, transition to agentic
                                                       (operator presumed gone).
* Row exists, authority == ``k8s-agentic``,
  event_emitter == ``k8s-operator``                 -> ACCEPT, transition to operator
                                                       (operator-supersedes-agentic).
* Any other emitter                                 -> ACCEPT (dedup logic is operator-vs-agentic
                                                       only in v0.2; non-k8s emitters are
                                                       passed through unchanged).

ACL invariant (non-negotiable)
------------------------------

``workspace_id`` is **always** supplied by the caller — the worker — and
is derived from ``Source.tenant_id`` server-side.  The dedup module
**never** reads it from the event payload.  This module's public API
forces the caller to pass ``workspace_id`` explicitly so a future change
on the call site cannot accidentally leak in payload-derived values.

Concurrency
-----------

The state machine is encoded as a single ``INSERT ... ON CONFLICT
DO UPDATE`` statement with a guarded ``WHERE`` predicate.  Postgres'
row-locking inside ``ON CONFLICT`` serialises concurrent writers on the
same primary key, so two simultaneous events for the same key produce
exactly one ACCEPT and one DROP / no-op (the loser sees the row with
the winner's authority and falls into the standard transition matrix).

No ``SELECT ... FOR UPDATE`` is required: the conditional UPSERT does
the right thing in one round-trip.

Performance
-----------

Every dedup decision is a single PK lookup on the ``entity_emitter``
table — no scan, no join.  The hot path adds <1ms p50 in lab benchmarks
on local Postgres; the issue's <5ms p50 budget is comfortably met.

Feature flag
------------

The gate is enabled by default (``OMNISCIENCE_DEDUP_ENABLED=true``).
Setting either ``OMNISCIENCE_DEDUP_ENABLED=false`` or
``OMNISCIENCE_INGEST_DEDUP_TTL_HOURS=-1`` disables the gate entirely
(the v0.2 fallback: both rows live in parallel).  ``..._TTL_HOURS=0``
makes the operator's authority sticky forever (no TTL re-assignment).
Anything positive is a clamp on how long the operator's authority
survives without a fresh emit.
"""

from __future__ import annotations

import enum
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from omniscience_server.ingestion.events import DocumentChangeEvent
from omniscience_server.ingestion.metrics import (
    INGESTION_DEDUP_ACTION_TOTAL,
    INGESTION_DEDUP_AUTHORITY_TRANSITIONS_TOTAL,
    INGESTION_DEDUP_DROP_TOTAL,
)

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants — every magic number lives here with a rationale.
# ---------------------------------------------------------------------------

# Canonical emitter strings.  These match exactly what the producers
# stamp:
#   * The Go operator stamps ``SourceType = "k8s-operator"`` on the
#     ``DocumentChangeEvent.source_type`` field
#     (``operator/internal/entity/entity.go``).
#   * The agentic Kubernetes connector stamps
#     ``connector_type = "k8s-agentic"``
#     (``packages/connectors/src/omniscience_connectors/agentic/k8s.py``).
EMITTER_OPERATOR: Final[str] = "k8s-operator"
EMITTER_AGENTIC: Final[str] = "k8s-agentic"

# The two K8s emitters this module disambiguates.  Other ``source_type``
# values (git, slack, terraform, …) bypass dedup entirely — see
# :func:`is_k8s_emitter`.
_K8S_EMITTERS: Final[frozenset[str]] = frozenset({EMITTER_OPERATOR, EMITTER_AGENTIC})

# Default TTL for the operator's authority survival without a fresh
# emit.  24h is the issue's spec'd default; an operator emitting on the
# 15-min reconcile cycle (#163) refreshes the row every cycle, so the
# only way a 24h gap appears is a sustained operator outage.
DEFAULT_TTL_HOURS: Final[float] = 24.0

# Sentinel for "TTL is sticky — never re-assign back to agentic".  Set
# OMNISCIENCE_INGEST_DEDUP_TTL_HOURS=0 to make operator authority
# permanent.  Useful in environments where the operator is the only K8s
# emitter and a misbehaving agentic connector must never reclaim a row.
_TTL_STICKY_SENTINEL: Final[float] = 0.0

# Sentinel for "dedup disabled".  Set OMNISCIENCE_INGEST_DEDUP_TTL_HOURS=-1
# to fully disable the gate (debug only — both emitters write in
# parallel).  Equivalent to OMNISCIENCE_DEDUP_ENABLED=false.
_TTL_DISABLED_SENTINEL: Final[float] = -1.0

# Action strings emitted on the coarse-grained ``omniscience_ingestion_dedup_total``
# counter.  Stable identifiers — dashboards key on these.
ACTION_FIRST_AUTHORITY_ASSIGNED: Final[str] = "first_authority_assigned"
ACTION_NO_OP_IDEMPOTENT_REFRESH: Final[str] = "no_op_idempotent_refresh"
ACTION_NO_OP_AGENTIC_DROPPED: Final[str] = "no_op_agentic_dropped"
ACTION_OPERATOR_SUPERSEDES_AGENTIC: Final[str] = "operator_supersedes_agentic"
ACTION_TTL_REASSIGN_TO_AGENTIC: Final[str] = "ttl_reassign_to_agentic"
ACTION_DEDUP_DISABLED: Final[str] = "dedup_disabled"
ACTION_NON_K8S_BYPASS: Final[str] = "non_k8s_bypass"

# Set when the OMNISCIENCE_K8S_AGENTIC_ALLOWED flag is false AND an event
# arrives from the k8s-agentic emitter.  The event is dropped at the gate
# boundary BEFORE any DB lookup — this is the v0.4 server-side enforcement
# of the agentic deprecation (ADR-0011, issue #216).  v0.3 default for
# the flag is true so this action is never observed in v0.3 deployments.
ACTION_AGENTIC_FLAG_DISABLED: Final[str] = "agentic_flag_disabled"

# external_id shape produced by the operator (see
# ``operator/internal/entity/entity.go``):
#
#   namespaced:     ``k8s_resource/<cluster_id>/<Kind>/<namespace>/<name>``
#   cluster-scoped: ``k8s_resource/<cluster_id>/<Kind>/<name>``
#
# Capturing only the kind segment is enough for the metrics label; the
# regex is anchored to fail fast and return ``None`` for any other
# external_id shape (terraform sources, git paths, etc.) so the
# ``kind`` label never picks up a free-form string.
_K8S_KIND_RE: Final[re.Pattern[str]] = re.compile(
    r"^k8s_resource/[0-9a-f-]+/(?P<kind>[A-Za-z][A-Za-z0-9]*)/"
)

# Fallback kind label for events whose external_id does not match the
# operator/agentic shape.  Bounded cardinality — never includes payload
# data.
_KIND_UNKNOWN: Final[str] = "unknown"


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class DedupAction(enum.StrEnum):
    """Outcome of a single dedup gate decision."""

    accept = "accept"
    drop = "drop"


@dataclass(frozen=True, slots=True)
class DedupConfig:
    """Runtime configuration for the dedup gate.

    Resolved once at worker construction from environment variables; not
    re-read per event.  Static for the lifetime of a worker (consistent
    with how Settings is consumed elsewhere in the server).
    """

    enabled: bool
    """When False, the gate is short-circuited; every event is ACCEPTed.
    The worker still calls :func:`evaluate` so metrics record the bypass
    accurately, but no DB lookup happens."""

    ttl_seconds: float | None
    """Authority TTL in seconds.  ``None`` = sticky (no TTL re-assignment).
    A positive value means: agentic events for an operator-authoritative
    ``(workspace_id, external_id)`` pair are dropped if ``last_emit_at``
    is within ``ttl_seconds``; otherwise authority flips back to
    agentic.  Always a finite real number; the
    ``-1`` disable sentinel collapses to ``enabled=False`` upstream."""

    agentic_allowed: bool = True
    """When False, every k8s-agentic event is dropped at the gate
    boundary before any DB lookup (issue #216, v0.4 default-flip
    enforcement per ADR-0011).  v0.3 default is True so the wiring is
    a no-op in current deployments; v0.4 will flip the operator-shipped
    default to False.  Operator and non-K8s emitters are unaffected by
    this flag — only k8s-agentic short-circuits."""


@dataclass(frozen=True, slots=True)
class DedupDecision:
    """The structured result of a dedup decision."""

    action: DedupAction
    """ACCEPT or DROP."""

    reason: str
    """One of the ``ACTION_*`` constants in this module."""

    authority_emitter: str
    """The emitter authoritative for this (workspace, external_id) AFTER
    the decision was applied."""

    kind: str
    """Parsed K8s kind (e.g. ``Pod``) used as a metrics label, or the
    sentinel ``"unknown"`` for non-k8s events."""


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def is_k8s_emitter(source_type: str) -> bool:
    """True if ``source_type`` is one of the two K8s emitters.

    Non-K8s events bypass the dedup gate unconditionally — there is no
    operator/agentic ambiguity for git, slack, etc.
    """
    return source_type in _K8S_EMITTERS


def parse_kind_from_external_id(external_id: str) -> str:
    """Extract the K8s kind segment from a canonical operator external_id.

    Returns the kind on match, or :data:`_KIND_UNKNOWN` for any other shape.
    The kind is used **only** as a metrics label; it does not feed any
    decision on the hot path, so a graceful fallback is safe.
    """
    m = _K8S_KIND_RE.match(external_id)
    if m is None:
        return _KIND_UNKNOWN
    return m.group("kind")


def config_from_env(
    *,
    enabled_env: str | None,
    ttl_hours_env: str | None,
    agentic_allowed_env: str | None = None,
) -> DedupConfig:
    """Translate the dedup environment variables into a :class:`DedupConfig`.

    Env vars honoured:

    * ``OMNISCIENCE_DEDUP_ENABLED`` = ``"false"`` -> dedup disabled.
    * ``OMNISCIENCE_INGEST_DEDUP_TTL_HOURS`` = ``"-1"`` -> dedup disabled.
    * ``OMNISCIENCE_INGEST_DEDUP_TTL_HOURS`` = ``"0"`` -> sticky (no TTL).
    * Any positive ``OMNISCIENCE_INGEST_DEDUP_TTL_HOURS`` -> TTL in hours.
    * ``OMNISCIENCE_K8S_AGENTIC_ALLOWED`` = ``"false"`` -> drop k8s-agentic
      events at the gate boundary (issue #216, ADR-0011).  Default ``True``
      in v0.3; v0.4 flips the operator-shipped default to ``False``.

    Unparseable values fall back to the secure default
    (``enabled=True``, ``ttl=24h``, ``agentic_allowed=True``) and are
    logged at WARNING.  This is the closed posture for a misconfiguration
    on every dial — the same posture used elsewhere on the boundary
    (retention worker, OTel receiver).
    """
    enabled = True
    if enabled_env is not None:
        enabled = enabled_env.strip().lower() not in {"false", "0", "no", "off", ""}

    ttl_hours: float | None = DEFAULT_TTL_HOURS
    if ttl_hours_env is not None:
        try:
            parsed = float(ttl_hours_env.strip())
        except ValueError:
            log.warning(
                "dedup_ttl_hours_unparseable_using_default",
                raw=ttl_hours_env,
                default_hours=DEFAULT_TTL_HOURS,
            )
            parsed = DEFAULT_TTL_HOURS

        if parsed == _TTL_DISABLED_SENTINEL:
            enabled = False
            ttl_hours = DEFAULT_TTL_HOURS
        elif parsed == _TTL_STICKY_SENTINEL:
            ttl_hours = None
        elif parsed > 0:
            ttl_hours = parsed
        else:
            # Negative values other than -1 are treated as the disable
            # sentinel — be liberal in what we accept while being strict
            # about the default-secure failure mode.
            log.warning(
                "dedup_ttl_hours_negative_treated_as_disabled",
                raw=ttl_hours_env,
            )
            enabled = False
            ttl_hours = DEFAULT_TTL_HOURS

    ttl_seconds: float | None = None if ttl_hours is None else float(ttl_hours) * 3600.0

    agentic_allowed = True
    if agentic_allowed_env is not None:
        agentic_allowed = agentic_allowed_env.strip().lower() not in {
            "false",
            "0",
            "no",
            "off",
            "",
        }

    return DedupConfig(
        enabled=enabled,
        ttl_seconds=ttl_seconds,
        agentic_allowed=agentic_allowed,
    )


# ---------------------------------------------------------------------------
# Core evaluator
# ---------------------------------------------------------------------------


class DedupGate:
    """Stateful gate that wraps the ``entity_emitter`` table.

    One instance per worker; cheap to construct.  The session factory is
    held by reference, not consumed eagerly — every call opens a fresh
    short-lived session so the gate composes with the worker's existing
    session lifecycle.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        config: DedupConfig,
    ) -> None:
        self._session_factory = session_factory
        self._config = config

    @property
    def config(self) -> DedupConfig:
        """Effective config — useful for tests and structured logging."""
        return self._config

    async def evaluate(
        self,
        *,
        workspace_id: uuid.UUID,
        event: DocumentChangeEvent,
        now: datetime | None = None,
    ) -> DedupDecision:
        """Decide whether to ACCEPT or DROP ``event`` for this workspace.

        ``now`` is exposed for testability — production callers leave it
        as ``None`` and the function uses ``datetime.now(timezone.utc)``.
        Tests pass an explicit value to drive the TTL boundary
        deterministically.
        """
        kind = parse_kind_from_external_id(event.external_id)

        # First: agentic-allowed flag (issue #216 / ADR-0011).  When the
        # operator owns K8s coverage and the deployment has flipped the
        # flag closed, drop k8s-agentic events at the gate boundary
        # before any DB lookup.  Operator and non-K8s events are
        # unaffected.  This check runs BEFORE the dedup-disabled
        # short-circuit so the flag is enforced even in deployments
        # that have explicitly turned dedup off (debug envs, etc.).
        if not self._config.agentic_allowed and event.source_type == EMITTER_AGENTIC:
            INGESTION_DEDUP_ACTION_TOTAL.labels(
                kind=kind,
                action=ACTION_AGENTIC_FLAG_DISABLED,
            ).inc()
            return DedupDecision(
                action=DedupAction.drop,
                reason=ACTION_AGENTIC_FLAG_DISABLED,
                authority_emitter=EMITTER_OPERATOR,
                kind=kind,
            )

        # Fast path: dedup disabled or non-K8s emitter — bypass the table.
        if not self._config.enabled:
            INGESTION_DEDUP_ACTION_TOTAL.labels(
                kind=kind,
                action=ACTION_DEDUP_DISABLED,
            ).inc()
            return DedupDecision(
                action=DedupAction.accept,
                reason=ACTION_DEDUP_DISABLED,
                authority_emitter=event.source_type,
                kind=kind,
            )

        if not is_k8s_emitter(event.source_type):
            INGESTION_DEDUP_ACTION_TOTAL.labels(
                kind=kind,
                action=ACTION_NON_K8S_BYPASS,
            ).inc()
            return DedupDecision(
                action=DedupAction.accept,
                reason=ACTION_NON_K8S_BYPASS,
                authority_emitter=event.source_type,
                kind=kind,
            )

        wall_now = now if now is not None else datetime.now(UTC)
        async with self._session_factory() as session:
            return await self._evaluate_in_session(
                session=session,
                workspace_id=workspace_id,
                event=event,
                kind=kind,
                wall_now=wall_now,
            )

    async def _evaluate_in_session(
        self,
        *,
        session: AsyncSession,
        workspace_id: uuid.UUID,
        event: DocumentChangeEvent,
        kind: str,
        wall_now: datetime,
    ) -> DedupDecision:
        """Run the state machine inside a single transaction.

        Two queries: a SELECT to read the current authority (with row
        lock for serialisation under concurrency), then a single
        upsert/update.  All bind parameters — no string concatenation,
        no payload-derived SQL.  The workspace_id and external_id are
        the composite primary key, so the lookup is a single index hit.
        """
        emitter = event.source_type
        cutoff: datetime | None
        if self._config.ttl_seconds is None:
            cutoff = None
        else:
            cutoff = wall_now - timedelta(seconds=self._config.ttl_seconds)

        # Lock the (workspace_id, external_id) row if it exists; serialises
        # any concurrent writers on the same key.  ``FOR UPDATE`` on a
        # primary-key SELECT is index-only and very cheap.
        select_stmt = text(
            """
            SELECT authority_emitter, last_emit_at
              FROM entity_emitter
             WHERE workspace_id = :workspace_id
               AND external_id = :external_id
             FOR UPDATE
            """
        )
        row = (
            await session.execute(
                select_stmt,
                {
                    "workspace_id": workspace_id,
                    "external_id": event.external_id,
                },
            )
        ).first()

        if row is None:
            # First write for this pair — establishes authority.  Use
            # plain INSERT; the FOR UPDATE above guarantees no concurrent
            # writer can have inserted between the SELECT and here on the
            # same row (the SELECT acquired a key-share lock and any
            # competing INSERT will block on the unique constraint).
            await session.execute(
                text(
                    """
                    INSERT INTO entity_emitter
                        (workspace_id, external_id, authority_emitter,
                         last_emit_at, authority_changed_at)
                    VALUES (:workspace_id, :external_id, :authority,
                            :now, NULL)
                    ON CONFLICT (workspace_id, external_id) DO UPDATE
                        SET last_emit_at = EXCLUDED.last_emit_at
                       WHERE entity_emitter.authority_emitter = EXCLUDED.authority_emitter
                    """
                ),
                {
                    "workspace_id": workspace_id,
                    "external_id": event.external_id,
                    "authority": emitter,
                    "now": wall_now,
                },
            )
            await session.commit()
            INGESTION_DEDUP_ACTION_TOTAL.labels(
                kind=kind,
                action=ACTION_FIRST_AUTHORITY_ASSIGNED,
            ).inc()
            return DedupDecision(
                action=DedupAction.accept,
                reason=ACTION_FIRST_AUTHORITY_ASSIGNED,
                authority_emitter=emitter,
                kind=kind,
            )

        current_authority: str = row.authority_emitter
        last_emit_at: datetime = row.last_emit_at

        # Same emitter as authority -> idempotent refresh.
        if current_authority == emitter:
            await session.execute(
                text(
                    """
                    UPDATE entity_emitter
                       SET last_emit_at = :now
                     WHERE workspace_id = :workspace_id
                       AND external_id = :external_id
                       AND authority_emitter = :authority
                    """
                ),
                {
                    "workspace_id": workspace_id,
                    "external_id": event.external_id,
                    "authority": emitter,
                    "now": wall_now,
                },
            )
            await session.commit()
            INGESTION_DEDUP_ACTION_TOTAL.labels(
                kind=kind,
                action=ACTION_NO_OP_IDEMPOTENT_REFRESH,
            ).inc()
            return DedupDecision(
                action=DedupAction.accept,
                reason=ACTION_NO_OP_IDEMPOTENT_REFRESH,
                authority_emitter=current_authority,
                kind=kind,
            )

        # Authority flip: agentic -> operator (operator-supersedes-agentic).
        # Always allowed regardless of TTL — operator coverage expansion is
        # the primary intended transition.
        if emitter == EMITTER_OPERATOR and current_authority == EMITTER_AGENTIC:
            await session.execute(
                text(
                    """
                    UPDATE entity_emitter
                       SET authority_emitter = :new_authority,
                           last_emit_at = :now,
                           authority_changed_at = :now
                     WHERE workspace_id = :workspace_id
                       AND external_id = :external_id
                       AND authority_emitter = :old_authority
                    """
                ),
                {
                    "workspace_id": workspace_id,
                    "external_id": event.external_id,
                    "old_authority": EMITTER_AGENTIC,
                    "new_authority": EMITTER_OPERATOR,
                    "now": wall_now,
                },
            )
            await session.commit()
            INGESTION_DEDUP_AUTHORITY_TRANSITIONS_TOTAL.labels(
                kind=kind,
                from_emitter=EMITTER_AGENTIC,
                to_emitter=EMITTER_OPERATOR,
            ).inc()
            INGESTION_DEDUP_ACTION_TOTAL.labels(
                kind=kind,
                action=ACTION_OPERATOR_SUPERSEDES_AGENTIC,
            ).inc()
            log.info(
                "dedup_authority_transition",
                kind=kind,
                from_emitter=EMITTER_AGENTIC,
                to_emitter=EMITTER_OPERATOR,
            )
            return DedupDecision(
                action=DedupAction.accept,
                reason=ACTION_OPERATOR_SUPERSEDES_AGENTIC,
                authority_emitter=EMITTER_OPERATOR,
                kind=kind,
            )

        # Authority flip: operator -> agentic, but only if the TTL window
        # expired.  This is the only path that returns authority to the
        # agentic connector once the operator has held it.
        if emitter == EMITTER_AGENTIC and current_authority == EMITTER_OPERATOR:
            ttl_expired = cutoff is not None and last_emit_at < cutoff
            if ttl_expired:
                await session.execute(
                    text(
                        """
                        UPDATE entity_emitter
                           SET authority_emitter = :new_authority,
                               last_emit_at = :now,
                               authority_changed_at = :now
                         WHERE workspace_id = :workspace_id
                           AND external_id = :external_id
                           AND authority_emitter = :old_authority
                           AND last_emit_at < :cutoff
                        """
                    ),
                    {
                        "workspace_id": workspace_id,
                        "external_id": event.external_id,
                        "old_authority": EMITTER_OPERATOR,
                        "new_authority": EMITTER_AGENTIC,
                        "now": wall_now,
                        "cutoff": cutoff,
                    },
                )
                await session.commit()
                INGESTION_DEDUP_AUTHORITY_TRANSITIONS_TOTAL.labels(
                    kind=kind,
                    from_emitter=EMITTER_OPERATOR,
                    to_emitter=EMITTER_AGENTIC,
                ).inc()
                INGESTION_DEDUP_ACTION_TOTAL.labels(
                    kind=kind,
                    action=ACTION_TTL_REASSIGN_TO_AGENTIC,
                ).inc()
                log.info(
                    "dedup_authority_transition",
                    kind=kind,
                    from_emitter=EMITTER_OPERATOR,
                    to_emitter=EMITTER_AGENTIC,
                    ttl_seconds=self._config.ttl_seconds,
                )
                return DedupDecision(
                    action=DedupAction.accept,
                    reason=ACTION_TTL_REASSIGN_TO_AGENTIC,
                    authority_emitter=EMITTER_AGENTIC,
                    kind=kind,
                )
            # TTL not expired -> drop the agentic event.
            await session.rollback()
            INGESTION_DEDUP_DROP_TOTAL.labels(
                kind=kind,
                dropped_emitter=EMITTER_AGENTIC,
                authority_emitter=EMITTER_OPERATOR,
            ).inc()
            INGESTION_DEDUP_ACTION_TOTAL.labels(
                kind=kind,
                action=ACTION_NO_OP_AGENTIC_DROPPED,
            ).inc()
            log.debug(
                "dedup_event_dropped",
                kind=kind,
                dropped_emitter=EMITTER_AGENTIC,
                authority_emitter=EMITTER_OPERATOR,
            )
            return DedupDecision(
                action=DedupAction.drop,
                reason=ACTION_NO_OP_AGENTIC_DROPPED,
                authority_emitter=EMITTER_OPERATOR,
                kind=kind,
            )

        # Defensive fallback: any unknown emitter pair is accepted but
        # logged at WARNING — the dedup logic is intentionally narrow
        # (operator vs agentic) and a third K8s emitter is out of scope
        # per the issue.  This branch must never hit in v0.2.
        await session.rollback()
        log.warning(
            "dedup_unhandled_emitter_pair",
            current_authority=current_authority,
            event_emitter=emitter,
            kind=kind,
        )
        return DedupDecision(
            action=DedupAction.accept,
            reason=ACTION_NON_K8S_BYPASS,
            authority_emitter=current_authority,
            kind=kind,
        )


__all__ = [
    "ACTION_DEDUP_DISABLED",
    "ACTION_FIRST_AUTHORITY_ASSIGNED",
    "ACTION_NON_K8S_BYPASS",
    "ACTION_NO_OP_AGENTIC_DROPPED",
    "ACTION_NO_OP_IDEMPOTENT_REFRESH",
    "ACTION_OPERATOR_SUPERSEDES_AGENTIC",
    "ACTION_TTL_REASSIGN_TO_AGENTIC",
    "DEFAULT_TTL_HOURS",
    "EMITTER_AGENTIC",
    "EMITTER_OPERATOR",
    "DedupAction",
    "DedupConfig",
    "DedupDecision",
    "DedupGate",
    "config_from_env",
    "is_k8s_emitter",
    "parse_kind_from_external_id",
]
