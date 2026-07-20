"""Process-local `AdmissionBackend` implementation (issue #350, AC-SCALE-1).

This is today's pre-#350 token-bucket algorithm (previously the module-level
`_buckets` dict in `rest/rate_limit.py`), refactored behind the
`AdmissionBackend` Protocol with identical bucket math — only the storage
key changed from `token_id` alone to `(lane, key)`, so existing single-lane
callers see byte-for-byte identical behaviour.

Explicitly an MVP/single-process implementation: state lives in this
process's memory only and is never shared across replicas. It is the
`admission_backend="disabled"` default (see `omniscience_core.config`) and
MUST NOT be used as a silent fallback when a shared backend is configured
but unreachable — see `admission.state_machine.FailClosedAdmissionController`
for the fail-closed behaviour that replaces the old ad hoc bypass.
"""

from __future__ import annotations

import time

from omniscience_server.admission.backend import AdmissionDecision, BackendHealth


class ProcessLocalAdmissionBackend:
    """In-memory token-bucket `AdmissionBackend`, keyed by `(lane, key)`."""

    def __init__(self) -> None:
        # (lane, key) -> (tokens_remaining, last_refill_monotonic)
        self._buckets: dict[tuple[str, str], tuple[float, float]] = {}

    def try_admit(
        self,
        *,
        lane: str,
        key: str,
        capacity: int,
        refill_per_second: float,
    ) -> AdmissionDecision:
        now = time.monotonic()
        bucket_key = (lane, key)
        cap = float(capacity)

        if bucket_key not in self._buckets:
            # Brand-new bucket — start full.
            self._buckets[bucket_key] = (cap - 1.0, now)
            return AdmissionDecision(allowed=True, retry_after_seconds=0.0)

        tokens, last_refill = self._buckets[bucket_key]
        elapsed = now - last_refill
        tokens = min(cap, tokens + elapsed * refill_per_second)

        if tokens >= 1.0:
            self._buckets[bucket_key] = (tokens - 1.0, now)
            return AdmissionDecision(allowed=True, retry_after_seconds=0.0)

        retry_after = (1.0 - tokens) / refill_per_second if refill_per_second > 0 else float("inf")
        self._buckets[bucket_key] = (tokens, now)
        return AdmissionDecision(allowed=False, retry_after_seconds=retry_after)

    def health(self) -> BackendHealth:
        return BackendHealth(
            healthy=True,
            detail="process-local (in-memory, not shared across replicas)",
        )

    def reset(self, *, lane: str, key: str) -> None:
        """Reset the bucket for one `(lane, key)` pair (test helper)."""
        self._buckets.pop((lane, key), None)

    def clear(self) -> None:
        """Clear every bucket (test helper)."""
        self._buckets.clear()


__all__ = ["ProcessLocalAdmissionBackend"]
