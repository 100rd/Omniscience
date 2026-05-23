"""Deterministic state-fingerprint hash for replay (issue #243).

The replay primitive promises that two replays at the same ``at_time``
yield the same byte-identical response.  We surface that promise as a
hash so the caller does not need to byte-compare two JSON blobs to
verify it — a CISO/GRC auditor just compares two short hex strings.

Hash algorithm
--------------

BLAKE2b truncated to 256 bits.  Rationale:

1. ``hashlib`` ships it in the stdlib (no new dep).
2. Faster than SHA-256 on every modern CPU.
3. 256 bits of output is plenty for a fingerprint that does not need
   to defend against adaptive collision attacks — the inputs come
   from the server's own response payload, never user-controlled.
4. The hex form is 64 chars wide — short enough to paste in an
   email/RFP appendix, long enough that accidental collisions are
   impossible in any realistic audit-log volume.

Canonical-JSON encoding
-----------------------

The fingerprint must be reproducible across Python versions, machine
architectures, and dict-iteration orders.  We canonicalise with::

    json.dumps(payload, sort_keys=True, separators=(",", ":"),
               ensure_ascii=False, default=_json_default)

* ``sort_keys=True`` — kills dict-insertion-order non-determinism.
* ``separators=(",", ":")`` — minimises whitespace differences.
* ``ensure_ascii=False`` — keeps non-ASCII characters intact (UTF-8
  is byte-stable when both serializer and hasher agree on encoding).
* ``default=_json_default`` — handles ``datetime``, ``UUID``, and
  ``Decimal`` by converting them to their canonical string form.

Encoding-stability guard
------------------------

The encoded bytes are UTF-8.  The hash domain is prefixed with
``b"omniscience-replay-v1:"`` so a future format revision (canonical
CBOR, schema-namespaced JSON) bumps to ``-v2:`` and never collides
with v1 hashes on the same payload.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final

#: Algorithm label persisted alongside the hex digest so a future
#: revision can be distinguished without a schema change.
FINGERPRINT_ALGORITHM: Final[str] = "blake2b-256-canonjson-v1"

#: Number of hex chars in a v1 fingerprint (32 bytes -> 64 hex chars).
FINGERPRINT_HEX_LENGTH: Final[int] = 64

#: Domain-separation prefix.  Bumping this string invalidates every
#: v1 hash — only do that when bumping :data:`FINGERPRINT_ALGORITHM`.
_DOMAIN_PREFIX: Final[bytes] = b"omniscience-replay-v1:"


def _json_default(value: Any) -> Any:
    """Convert non-JSON-native scalars to canonical string form.

    ``datetime`` -> ISO-8601 with UTC offset.  Naive datetimes are
    rejected explicitly so a stray non-tz-aware value cannot quietly
    poison a fingerprint with locale-dependent text.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise TypeError(
                "state-fingerprint payload carries a naive datetime; "
                "all bitemporal timestamps must be UTC-aware (ADR-0008 §1)"
            )
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        # ``str(Decimal)`` preserves the canonical decimal repr; ``float``
        # would lose precision.  Auditors care about exact equality.
        return str(value)
    if isinstance(value, bytes):
        # Stable hex repr — same hash regardless of how the bytes were
        # constructed.  Realistic payloads should not carry raw bytes
        # but defensive coverage is cheap.
        return value.hex()
    raise TypeError(
        f"state-fingerprint payload contains unsupported type {type(value).__name__!r}"
    )


def _canonical_json(payload: Any) -> bytes:
    """Return canonical-JSON UTF-8 bytes for ``payload``.

    Centralised so call sites in tests and production share a single
    serialiser — any drift between them would silently invalidate the
    deterministic-hash contract.
    """
    text = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )
    return text.encode("utf-8")


def compute_state_fingerprint(payload: Any) -> str:
    """Compute the v1 state-fingerprint hex digest for ``payload``.

    The input is the response dict the original tool returned.
    Returns a 64-char lowercase hex string.  Pure function — invoking
    it twice with equivalent inputs (modulo dict ordering) returns the
    same string.

    Mutability note
    ---------------

    Callers MUST NOT mutate ``payload`` after computing the
    fingerprint; the hash binds to the in-memory structure at the
    moment of the call.  In practice handlers compute the fingerprint
    immediately before logging + returning, so the window is
    structurally tiny.
    """
    hasher = hashlib.blake2b(digest_size=32)
    hasher.update(_DOMAIN_PREFIX)
    hasher.update(_canonical_json(payload))
    return hasher.hexdigest()


__all__ = [
    "FINGERPRINT_ALGORITHM",
    "FINGERPRINT_HEX_LENGTH",
    "compute_state_fingerprint",
]
